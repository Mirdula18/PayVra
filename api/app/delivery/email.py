"""The Resend transport. The smallest thing that puts a real message in a real inbox.

Scope is deliberately one provider and one channel (FR-10.1). Delivery receipts (FR-10.3), a
second channel (FR-10.2), SMS and WhatsApp are explicit non-goals.

**Nothing here decides whether to send.** It is handed a recipient and a body and it either
delivers them or raises. The decision was made by ``guardrails.gate`` and enforced by
``delivery.sender.assert_sendable``; a transport that also had an opinion about whether to send
would be a second place where policy lives.

**Two safety properties, both failing closed:**

1. Sending is disabled unless ``RESEND_TO_OVERRIDE`` is set, so a system with credentials but no
   configured recipient cannot mail anyone.
2. Every message goes to that override address, never to the counterparty's stored email. The
   seeded book is full of realistic-looking addresses on a reserved domain; the gap between "demo
   data" and "someone's actual inbox" is one misconfiguration wide, and it is not a gap worth
   leaving open for convenience.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.config import settings
from app.exceptions import PayvraError

logger = logging.getLogger(__name__)

RESEND_ENDPOINT = "https://api.resend.com/emails"

#: Short. A send sits between a gate verdict and that verdict going stale (VERDICT_MAX_AGE is five
#: minutes), so a transport that hangs for thirty seconds per account would invalidate the very
#: authorisation it is acting on.
REQUEST_TIMEOUT_SECONDS = 15.0


class DeliveryError(PayvraError):
    """The message was not delivered. **Always recoverable: the action stays approved.**

    Raised for a refused send, a transport fault, or a missing configuration. The caller must not
    mark anything executed on this path -- ``guardrails/gate.py``'s rule is that the audit log may
    under-claim but must never over-claim, and a failed send that recorded success would be the
    purest form of over-claiming available.
    """


class DeliveryNotConfigured(DeliveryError):
    """No API key, or no override recipient. Distinct so a caller can tell "off" from "broken"."""


@dataclass(frozen=True)
class SendResult:
    """Proof of delivery, from the provider. What gets persisted on the message row."""

    provider_message_id: str
    to: str
    provider: str = "resend"


def is_configured() -> bool:
    """Whether a send could be attempted at all. Both halves are required."""
    key = settings.resend_api_key
    return bool(key and not key.startswith("dummy") and settings.resend_to_override)


def recipient_for(contact_email: str | None) -> str:
    """The address a message will actually go to.

    Always the override. ``contact_email`` is taken only so callers read naturally and so the real
    intended recipient can be logged beside the substitution -- it is never used as a destination.
    """
    del contact_email
    return settings.resend_to_override


def send_email(*, to: str, subject: str, body: str) -> SendResult:
    """Deliver one email. Raises :class:`DeliveryError` on anything short of provider acceptance.

    No retry. A 4xx will not change on a second attempt, and a 5xx or timeout is ambiguous --
    Resend may have accepted and sent the message before failing to tell us. Retrying an ambiguous
    send risks mailing the same counterparty twice, which is a frequency-cap breach the gate cannot
    see. The action stays approved and the next run re-gates it fresh, which is the safe direction.
    """
    if not is_configured():
        raise DeliveryNotConfigured(
            "email delivery is off: set RESEND_API_KEY and RESEND_TO_OVERRIDE"
        )

    payload = {
        "from": settings.resend_from,
        "to": [to],
        "subject": subject or "Invoice reminder",
        "text": body,
    }

    try:
        response = httpx.post(
            RESEND_ENDPOINT,
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            json=payload,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except httpx.RequestError as exc:
        raise DeliveryError(f"could not reach Resend: {exc}") from exc

    if response.status_code >= 400:
        # Resend's 403 for an unverified recipient is the common one and its message names the
        # only address it will accept, so it is worth surfacing verbatim rather than summarising.
        detail = _error_detail(response)
        raise DeliveryError(f"Resend {response.status_code}: {detail}")

    try:
        message_id = str(response.json()["id"])
    except (KeyError, ValueError) as exc:
        # Accepted but unparseable. Treated as a failure because we cannot record *which* message
        # was sent, and an execution record without a provider id cannot be reconciled later.
        raise DeliveryError(f"Resend accepted the send but returned no id: {exc}") from exc

    logger.info("email delivered to=%s provider_message_id=%s", to, message_id)
    return SendResult(provider_message_id=message_id, to=to)


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    return str(body.get("message") or body)[:300]


__all__ = [
    "DeliveryError",
    "DeliveryNotConfigured",
    "SendResult",
    "is_configured",
    "recipient_for",
    "send_email",
]
