"""Webhook signature verification and payload extraction.

**Signature is verified over the RAW request body, before any JSON parsing.** Parsing first and
re-serialising would change whitespace and key order, so the bytes signed would not be the bytes
checked — the signature would fail for honest payloads and, worse, the parse itself would be
running on unverified input.

``hmac.compare_digest`` throughout, never ``==``: a naive comparison returns early on the first
differing byte, which leaks the correct prefix through timing.

**The event id lives in a header, not the body.** See :data:`EVENT_ID_HEADER`.

**Never log a full payload.** It carries counterparty PII — names, emails, phone numbers. Log the
event id and type only (agents/razorpay-integration.md hard rule 5).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

SIGNATURE_HEADER = "X-Razorpay-Signature"

# The event id is a HEADER, not a body field. Razorpay's webhook envelope is
# ``{entity, account_id, event, contains, payload, created_at}`` -- there is no top-level ``id``,
# and Razorpay documents ``x-razorpay-event-id`` as the value that is unique per event and is the
# thing to deduplicate on. Reading ``payload["id"]`` instead yields an empty string on every
# genuine delivery, and rejecting on that would make Razorpay retry a valid event forever.
EVENT_ID_HEADER = "X-Razorpay-Event-Id"

# Prefix for the derived key used when the header is missing, so a fallback is obvious in the
# database and in a log line rather than looking like a real Razorpay id.
FALLBACK_EVENT_ID_PREFIX = "sha256:"

# Link events and their invoice-level twins. agents/razorpay-integration.md maps both to the same
# handling, since an `invoice.*` event carries the same reference_id.
SETTLING_EVENTS = ("payment_link.paid", "invoice.paid")
PARTIAL_EVENTS = ("payment_link.partially_paid", "invoice.partially_paid")
EXPIRY_EVENTS = ("payment_link.expired", "invoice.expired")
CANCEL_EVENTS = ("payment_link.cancelled",)

HANDLED_EVENTS = SETTLING_EVENTS + PARTIAL_EVENTS + EXPIRY_EVENTS + CANCEL_EVENTS


def verify_signature(raw: bytes, signature: str, secret: str) -> bool:
    """HMAC-SHA256 over the raw body, compared in constant time.

    Returns False rather than raising on a missing signature or secret: an unsigned request is
    simply not authentic, and treating it as an error would make it look like our bug in the logs.
    """
    if not signature or not secret:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def fallback_event_id(raw: bytes) -> str:
    """A stable dedupe key derived from the raw body, for a verified request with no id header.

    Razorpay redelivers the *same* event with the same body, so hashing those bytes dedupes a
    replay exactly as well as the real id would. Two genuinely different events would have to
    serialise identically to collide, and the envelope carries ``created_at`` and entity ids, so
    that does not happen in practice.

    Hashing the raw bytes, not the parsed payload: re-serialising would reorder keys and change
    whitespace, so the same event could hash two different ways.
    """
    return FALLBACK_EVENT_ID_PREFIX + hashlib.sha256(raw).hexdigest()


def _header(headers: Mapping[str, str], name: str) -> str:
    """Case-insensitive header lookup.

    Starlette's ``Headers`` is already case-insensitive, but a plain dict is not. Handling both
    keeps this testable without constructing a request, and HTTP header names are case-insensitive
    regardless of what any one client happens to send.
    """
    value = headers.get(name)
    if value:
        return value.strip()
    lowered = name.lower()
    for key, candidate in headers.items():
        if key.lower() == lowered and candidate:
            return candidate.strip()
    return ""


def resolve_event_id(headers: Mapping[str, str], raw: bytes) -> tuple[str, bool]:
    """Return ``(event_id, from_header)`` for a request whose signature already verified.

    Always returns a usable, non-empty key. The boolean is for logging only: the caller must
    treat both cases as processable, because by the time this is called the payload has been
    proven to come from Razorpay, and refusing a genuine event is the one failure mode that
    guarantees an infinite retry loop.
    """
    value = _header(headers, EVENT_ID_HEADER)
    if value:
        return value, True
    return fallback_event_id(raw), False


@dataclass(frozen=True)
class WebhookFacts:
    """The few fields we act on, lifted out of a payload we otherwise never log or store loosely."""

    event_id: str
    event_type: str
    reference_id: str | None
    razorpay_link_id: str | None
    invoice_id_note: str | None
    amount_paid_paise: int
    amount_paise: int

    @property
    def is_handled(self) -> bool:
        return self.event_type in HANDLED_EVENTS


def _entity(payload: dict[str, Any]) -> dict[str, Any]:
    """Pull the payment_link (or invoice) entity out of Razorpay's nested envelope."""
    container = payload.get("payload")
    if not isinstance(container, dict):
        return {}
    for key in ("payment_link", "invoice", "payment"):
        holder = container.get(key)
        if isinstance(holder, dict):
            entity = holder.get("entity")
            if isinstance(entity, dict):
                return entity
    return {}


def extract(payload: dict[str, Any], *, event_id: str) -> WebhookFacts:
    """Read the handful of fields reconciliation needs. Tolerant of shape drift by design.

    Razorpay adds fields over time; a strict schema here would 4xx an unrecognised-but-valid
    event, and Razorpay retries a non-2xx forever. Missing values become None and the caller
    decides, rather than the parser refusing.

    ``event_id`` is passed in rather than read from ``payload``: it arrives in the
    ``x-razorpay-event-id`` header and does not exist in the body. Use :func:`resolve_event_id`
    to obtain it.
    """
    entity = _entity(payload)
    notes = entity.get("notes") if isinstance(entity.get("notes"), dict) else {}

    return WebhookFacts(
        event_id=event_id,
        event_type=str(payload.get("event", "")),
        reference_id=_as_str(entity.get("reference_id")),
        razorpay_link_id=_as_str(entity.get("id")),
        invoice_id_note=_as_str(notes.get("invoice_id")) if notes else None,
        amount_paid_paise=_as_int(entity.get("amount_paid")),
        amount_paise=_as_int(entity.get("amount")),
    )


def _as_str(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def safe_log_fields(facts: WebhookFacts) -> dict[str, str]:
    """Exactly what may go in a log line. No customer block, no notes, no amounts tied to a name."""
    return {"event_id": facts.event_id, "event_type": facts.event_type}
