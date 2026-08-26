"""Payment link lifecycle: create, notify, cancel, regenerate (FR-9).

``reference_id`` carries the merchant's invoice number, which is what turns reconciliation from a
matching problem into one indexed lookup when the webhook arrives (ADR-006).

**It cannot be the bare invoice number on every link, though.** Razorpay enforces uniqueness on
``reference_id`` per account and rejects a reuse with ``400 BAD_REQUEST_ERROR``. The first link for
an invoice therefore carries the clean invoice number; each regeneration (FR-9.4) carries a
``-R2``, ``-R3``, ... suffix -- see :func:`next_reference_id`. This was found by
``scripts/verify_razorpay`` against the live test API, after the stubbed transport had happily
accepted the duplicate; the runbook exists for exactly this class of wrong assumption.

Reconciliation is unaffected by the suffix. ``_match_invoice`` resolves the stored link row first
and ``notes.invoice_id`` second, both of which are exact regardless of reference, and its
``reference_id`` fallback strips the suffix before matching.

**Test-mode link budget.** Razorpay caps standard Payment Links at 30 per business in test mode,
and that cap is what the demo runs on. Four things keep us under it:

1. **Links are created lazily**, only when an action actually needs one at dispatch. The seed
   creates zero — 120 seed invoices would blow the cap six times over on `make seed` alone.
2. **Idempotency is enforced against our own database first.** A repeat request for the same
   invoice + amount + purpose returns the stored link without calling Razorpay at all, so a
   retried dispatch cannot consume budget.
3. **A hard preflight ceiling** (:data:`LINK_BUDGET`) below the real cap, checked before any
   create. Hitting it raises :class:`LinkBudgetExceeded` and the caller requeues rather than
   failing the invoice.
4. **Settled invoices have their links cancelled** by ``link_hygiene``, which keeps the *active*
   set small even though cancelled links still count against a total-created cap.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.clock import now_utc
from app.exceptions import PayvraError
from app.models.contact import Contact
from app.models.invoice import Invoice
from app.models.payment_link import PaymentLink
from app.razorpay.client import RazorpayClient, idempotency_key

logger = logging.getLogger(__name__)

# Razorpay's own test-mode ceiling (ADR-006 "Known constraints").
RAZORPAY_TEST_MODE_LINK_CAP = 30

# Ours, with headroom. The gap is deliberate: a demo where someone has also clicked around the
# Razorpay dashboard should not discover the cap mid-pitch.
LINK_BUDGET = 25

# How long a link stays payable. Long enough that a finance team can process it in their next
# payment run; short enough that link_hygiene has something to do on stage.
DEFAULT_EXPIRY_DAYS = 14

# Regenerate a link this close to expiry while the invoice is still collectable (FR-9.4).
REGENERATE_WITHIN_HOURS = 48

# Razorpay rejects expire_by less than 15 minutes out.
MIN_EXPIRY_MINUTES = 20

# Link states that mean the link can still take money.
LIVE_LINK_STATUSES = ("created", "partially_paid")

# Separates the invoice number from the regeneration counter in a reference_id (FR-9.4).
REGENERATION_SUFFIX = "-R"

# Only a trailing "-R" plus digits is a suffix we generated. Anchored so an invoice number that
# legitimately ends in something like "-R" without a counter is left alone.
_REGENERATION_RE = re.compile(rf"{re.escape(REGENERATION_SUFFIX)}\d+$")


class LinkPurpose(StrEnum):
    """Part of the idempotency key, so it is what distinguishes "the same link" from a new one."""

    COLLECTION = "collection"
    REGENERATION = "regeneration"
    INSTALMENT = "instalment"


class LinkBudgetExceeded(PayvraError):
    """The test-mode link budget is exhausted. Requeue; do not fail the invoice."""


@dataclass(frozen=True)
class LinkResult:
    link: PaymentLink
    created: bool  # False when an existing link was reused rather than a new one created


def links_used(db: Session, merchant_id: uuid.UUID) -> int:
    """Payment links this merchant has ever created. Counted against the total-created cap.

    Cancelled and expired links are included on purpose: the test-mode cap is not documented as
    a concurrency limit, so the safe reading is that it counts everything ever made.
    """
    return int(
        db.execute(
            select(func.count())
            .select_from(PaymentLink)
            .join(Invoice, Invoice.id == PaymentLink.invoice_id)
            .where(Invoice.merchant_id == merchant_id)
        ).scalar_one()
    )


def next_reference_id(db: Session, invoice: Invoice) -> str:
    """The ``reference_id`` for the next link on this invoice. Unique per link, per FR-9.4.

    Razorpay rejects a duplicate ``reference_id`` outright, so the first link carries the bare
    invoice number and every subsequent one is suffixed ``-R2``, ``-R3``, and so on. The first
    link keeps the clean number deliberately: that is the value a merchant recognises on a
    Razorpay dashboard row, and the common case should not be made ugly to accommodate the rare
    one.

    Numbered from the count of links already stored for the invoice rather than from a parsed
    suffix, because the count is what the unique index actually protects. A create that fails at
    Razorpay writes no row, so the next attempt recomputes the same reference and retries cleanly.
    """
    existing = int(
        db.execute(
            select(func.count())
            .select_from(PaymentLink)
            .where(PaymentLink.invoice_id == invoice.id)
        ).scalar_one()
    )
    if existing == 0:
        return invoice.invoice_number
    return f"{invoice.invoice_number}{REGENERATION_SUFFIX}{existing + 1}"


def base_reference_id(reference_id: str) -> str:
    """Strip a ``-R<n>`` regeneration suffix, returning the invoice number underneath.

    Used by reconciliation's last-resort fallback so a regenerated link still resolves when the
    link row and ``notes.invoice_id`` are both unavailable. Anything not matching the suffix shape
    is returned untouched -- an invoice number is free to contain a hyphen and an R, and only a
    trailing ``-R`` followed by digits is ours.
    """
    return _REGENERATION_RE.sub("", reference_id)


def _primary_contact(db: Session, invoice: Invoice) -> Contact | None:
    return db.execute(
        select(Contact)
        .where(Contact.counterparty_id == invoice.counterparty_id, Contact.is_stale.is_(False))
        .order_by(Contact.is_primary.desc(), Contact.created_at)
        .limit(1)
    ).scalar_one_or_none()


def build_payload(
    invoice: Invoice,
    contact: Contact | None,
    *,
    amount_paise: int,
    expire_by: datetime,
    accept_partial: bool,
    reference_id: str,
) -> dict[str, object]:
    """The exact create-link body from agents/razorpay-integration.md.

    ``notify`` and ``reminder_enable`` are **both False, deliberately**. PAYVRA owns the messaging
    sequence and the guardrail gate. If Razorpay also sent reminders they would bypass our
    time-window check (nothing outside 08:00-19:00 IST), our frequency cap (2/week, 6/lifetime),
    and our audit log — every one of which is a compliance claim we make to a judge. A reminder we
    did not gate is a message we cannot account for, which breaks the entire compliance story.
    Do not enable these to "improve delivery".
    """
    customer: dict[str, object] = {}
    if contact is not None:
        customer = {
            "name": contact.name,
            **({"email": contact.email} if contact.email else {}),
            **({"contact": contact.phone} if contact.phone else {}),
        }

    return {
        "amount": amount_paise,
        "currency": "INR",
        "accept_partial": accept_partial,
        # THE reconciliation key. Carries the merchant's invoice number through to the webhook.
        # Required rather than derived here: Razorpay rejects a duplicate, so uniqueness is
        # decided once by next_reference_id() and a caller must not be able to skip that.
        "reference_id": reference_id,
        "description": f"Invoice {invoice.invoice_number}",
        "customer": customer,
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
        "expire_by": int(expire_by.timestamp()),
        # Belt and braces: if reference_id is ever absent from a payload, these still identify
        # the invoice without a lookup by amount.
        "notes": {
            "invoice_id": str(invoice.id),
            "merchant_id": str(invoice.merchant_id),
        },
    }


def create_link(
    db: Session,
    client: RazorpayClient,
    invoice: Invoice,
    *,
    amount_paise: int | None = None,
    purpose: LinkPurpose = LinkPurpose.COLLECTION,
    accept_partial: bool = False,
    expire_by: datetime | None = None,
) -> LinkResult:
    """Create a payment link, or return the existing one for this invoice+amount+purpose.

    Idempotency is resolved against **our** database before Razorpay is called. That is the
    mechanism that actually prevents a duplicate — the same pattern as webhook dedupe, where the
    unique constraint is the control and application logic is not (ADR-006). The key also travels
    as ``X-Razorpay-Idempotency-Key`` so Razorpay can deduplicate on its side too.
    """
    amount = amount_paise if amount_paise is not None else invoice.outstanding_paise
    if amount <= 0:
        raise ValueError(f"cannot create a link for {amount} paise on {invoice.invoice_number}")

    key = idempotency_key(invoice.id, amount, purpose.value)

    existing = db.execute(
        select(PaymentLink).where(PaymentLink.idempotency_key == key)
    ).scalar_one_or_none()
    if existing is not None:
        logger.info(
            "reusing payment link invoice=%s purpose=%s", invoice.invoice_number, purpose.value
        )
        return LinkResult(link=existing, created=False)

    used = links_used(db, invoice.merchant_id)
    if used >= LINK_BUDGET:
        raise LinkBudgetExceeded(
            f"{used} links already created against a budget of {LINK_BUDGET} "
            f"(Razorpay test-mode cap is {RAZORPAY_TEST_MODE_LINK_CAP}); requeue this action"
        )

    expiry = expire_by or (now_utc() + timedelta(days=DEFAULT_EXPIRY_DAYS))
    minimum = now_utc() + timedelta(minutes=MIN_EXPIRY_MINUTES)
    if expiry < minimum:
        expiry = minimum

    reference = next_reference_id(db, invoice)
    payload = build_payload(
        invoice,
        _primary_contact(db, invoice),
        amount_paise=amount,
        expire_by=expiry,
        accept_partial=accept_partial,
        reference_id=reference,
    )
    response = client.create_payment_link(payload, idempotency=key)

    link = PaymentLink(
        id=uuid.uuid4(),
        invoice_id=invoice.id,
        razorpay_link_id=str(response["id"]),
        short_url=str(response.get("short_url", "")),
        amount_paise=amount,
        # Store what was actually sent, not the invoice number: reconciliation's fallback
        # compares against this row, so a drift here would silently unmatch a regenerated link.
        reference_id=reference,
        status=str(response.get("status", "created")),
        expire_by=expiry,
        accept_partial=accept_partial,
        idempotency_key=key,
    )
    db.add(link)
    db.flush()
    logger.info(
        "created payment link invoice=%s razorpay_link_id=%s purpose=%s",
        invoice.invoice_number,
        link.razorpay_link_id,
        purpose.value,
    )
    return LinkResult(link=link, created=True)


def notify_link(client: RazorpayClient, link: PaymentLink, medium: str) -> None:
    """Ask Razorpay to resend an existing link (FR-9.3).

    Explicit and gated: this is called only after ``guardrails.gate`` approved the touch, which is
    exactly what distinguishes it from the automatic reminders we keep switched off.
    """
    client.notify(link.razorpay_link_id, medium)
    logger.info("notified link=%s medium=%s", link.razorpay_link_id, medium)


def cancel_link(db: Session, client: RazorpayClient, link: PaymentLink) -> bool:
    """Cancel one link at Razorpay and record it locally. Returns False if it was already dead.

    Best effort by design: a link that cannot be cancelled is cosmetic, while the settle it
    follows is not. See ``reconciliation.settle`` for why this is never inside the settle
    transaction.
    """
    if link.status not in LIVE_LINK_STATUSES:
        return False
    try:
        response = client.cancel(link.razorpay_link_id)
        link.status = str(response.get("status", "cancelled"))
    except PayvraError as exc:
        # Leave the row live so link_hygiene retries it. Never re-raise into a settle.
        logger.warning("could not cancel link=%s: %s", link.razorpay_link_id, exc)
        return False
    db.flush()
    return True


def links_for_invoice(
    db: Session, invoice_id: uuid.UUID, *, live_only: bool = False
) -> list[PaymentLink]:
    stmt = select(PaymentLink).where(PaymentLink.invoice_id == invoice_id)
    if live_only:
        stmt = stmt.where(PaymentLink.status.in_(LIVE_LINK_STATUSES))
    return list(db.execute(stmt.order_by(PaymentLink.created_at)).scalars())


def regenerate_if_needed(
    db: Session, client: RazorpayClient, invoice: Invoice, *, now: datetime | None = None
) -> LinkResult | None:
    """Regenerate a link nearing expiry, but only while the invoice is still collectable (FR-9.4).

    Returns ``None`` when nothing needed doing. Deliberately silent on settled and stopped
    invoices: regenerating a link for someone who has already paid, or who we have permanently
    stopped contacting, is the same class of mistake as messaging them.
    """
    from app.enums import PaymentStatus, RecoveryState

    moment = now or now_utc()
    if invoice.payment_status in (PaymentStatus.PAID.value, PaymentStatus.WRITTEN_OFF.value):
        return None
    if invoice.recovery_state in (RecoveryState.SETTLED.value, RecoveryState.STOPPED.value):
        return None
    if invoice.outstanding_paise <= 0:
        return None

    live = links_for_invoice(db, invoice.id, live_only=True)
    if not live:
        return None
    if all(link.expire_by - moment > timedelta(hours=REGENERATE_WITHIN_HOURS) for link in live):
        return None

    return create_link(
        db,
        client,
        invoice,
        amount_paise=invoice.outstanding_paise,
        purpose=LinkPurpose.REGENERATION,
        accept_partial=any(link.accept_partial for link in live),
    )
