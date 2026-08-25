"""Build a :class:`MessageContext` from the database.

The generation layer itself is deliberately session-free -- templates, validator and drafter all
take a plain context -- so this is the one module that knows about ORM objects. Keeping the seam
here is what lets the whole of Phase 5 be tested without a database, and it is also what stops a
model call from ever being made while a session is open in a request.

Two of the five required elements are *looked up*, not passed in, and both raise rather than
degrade if missing:

* **The payment link.** A reminder without one is a reminder that cannot be paid, and it fails
  ``policy_content.find_missing_elements`` anyway. Better to fail loudly here, where the caller
  can create a link, than to draft a message that the gate will silently block.
* **The opt-out token.** Sending without a working opt-out is a DPDP problem, not a formatting
  one. There is no safe default, so there is no default.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.enums import Channel
from app.exceptions import PayvraError
from app.models.consent import Consent
from app.models.counterparty import Counterparty
from app.models.invoice import Invoice
from app.models.merchant import Merchant
from app.models.payment_link import PaymentLink
from app.schemas.generation import LANGUAGES, Language, MessageContext, ToneTier

logger = logging.getLogger(__name__)

# Mirrors links.LIVE_LINK_STATUSES. A cancelled or expired link is not payable, so quoting one
# would produce a message that looks complete and cannot be acted on.
LIVE_LINK_STATUSES = ("created", "partially_paid")


class ContextIncomplete(PayvraError):
    """The invoice cannot be drafted for yet. Always actionable by the caller."""


def resolve_language(counterparty: Counterparty) -> Language:
    """The counterparty's preference, falling back to English (FR-8.5).

    Regional languages are FR-8.6 and explicitly P2, so a counterparty marked ``ta`` or ``gu``
    gets English rather than an untested template -- a message in the wrong language is a
    cosmetic failure, a missing template is an unsendable invoice.
    """
    preferred = (counterparty.preferred_language or "en").lower()
    if preferred in LANGUAGES:
        return preferred
    logger.debug("unsupported language %r; using en", preferred)
    return "en"


def _clamp_tier(value: int) -> ToneTier:
    """Clamp to the four defined tiers.

    ``invoices.current_tone_tier`` is a plain ``SmallInteger``, so nothing in the type system
    stops a 0 or a 7 reaching here. Clamping rather than raising is right: an out-of-range tier
    is a scoring bug, and refusing to draft would escalate it into an unsendable invoice.
    """
    if value <= 1:
        return 1
    if value == 2:
        return 2
    if value == 3:
        return 3
    return 4


def opt_out_url(db: Session, counterparty_id: uuid.UUID, channel: Channel) -> str:
    """The recipient's opt-out link for this channel.

    Per channel, because consent is recorded per channel: opting out of SMS is not opting out of
    email, and one token covering both would silently over-apply a preference.
    """
    token = db.execute(
        select(Consent.opt_out_token).where(
            Consent.counterparty_id == counterparty_id,
            Consent.channel == channel.value,
        )
    ).scalar_one_or_none()
    if not token:
        raise ContextIncomplete(
            f"no {channel.value} consent record for counterparty {counterparty_id}; "
            "cannot build a working opt-out link"
        )
    return f"{settings.public_base_url.rstrip('/')}/opt-out/{token}"


def live_payment_link(db: Session, invoice_id: uuid.UUID) -> str | None:
    """The newest payable link for this invoice, or None."""
    return db.execute(
        select(PaymentLink.short_url)
        .where(
            PaymentLink.invoice_id == invoice_id,
            PaymentLink.status.in_(LIVE_LINK_STATUSES),
            PaymentLink.short_url != "",
        )
        .order_by(PaymentLink.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def build_context(
    db: Session,
    invoice: Invoice,
    *,
    channel: Channel,
    tone_tier: ToneTier | None = None,
    payment_link_url: str | None = None,
    promise_context: str | None = None,
) -> MessageContext:
    """Assemble everything a message is written from. All reads, no writes, no LLM.

    ``payment_link_url`` may be passed by a caller that has just created one, so a dispatch loop
    does not have to flush and re-query between creating the link and drafting the message.
    """
    merchant = db.get(Merchant, invoice.merchant_id)
    if merchant is None:  # pragma: no cover - FK guarantees this
        raise ContextIncomplete(f"merchant {invoice.merchant_id} not found")
    counterparty = db.get(Counterparty, invoice.counterparty_id)
    if counterparty is None:  # pragma: no cover - FK guarantees this
        raise ContextIncomplete(f"counterparty {invoice.counterparty_id} not found")

    link = payment_link_url or live_payment_link(db, invoice.id)
    if not link:
        raise ContextIncomplete(
            f"invoice {invoice.invoice_number} has no live payment link; create one before "
            "drafting (links are created lazily at dispatch — see razorpay/links.py)"
        )

    tier = _clamp_tier(tone_tier if tone_tier is not None else invoice.current_tone_tier)

    return MessageContext(
        merchant_name=merchant.name,
        counterparty_name=counterparty.name,
        invoice_number=invoice.invoice_number,
        outstanding_paise=invoice.outstanding_paise,
        due_date=invoice.due_date,
        days_past_due=invoice.days_past_due,
        payment_link_url=link,
        opt_out_url=opt_out_url(db, counterparty.id, channel),
        channel=channel,
        language=resolve_language(counterparty),
        tone_tier=tier,
        touch_count=invoice.touch_count,
        promise_context=promise_context,
        invoice_id=invoice.id,
    )


__all__ = [
    "ContextIncomplete",
    "build_context",
    "live_payment_link",
    "opt_out_url",
    "resolve_language",
]
