"""Manual reconciliation: paid offline, and marked disputed (FR-13.6, api-contracts.md).

Not every payment arrives through Razorpay. Indian B2B settles a great deal by cheque, NEFT and
RTGS, and a merchant who has just seen a UTR land in their bank statement needs to stop the
outreach *now*, not after a webhook that will never come.

``mark_paid_offline`` calls the **identical** :func:`~app.reconciliation.settle.settle_invoice`
with ``source='manual'``. One settle path, not two — so the offline route revokes pending actions,
closes promises and writes its audit entry by exactly the same code the webhook uses, and a test
can assert both produce identical database state.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.log import record as audit_record
from app.clock import IST, now_utc
from app.enums import ActorType, RecoveryState, StopReason, UnpaidCause
from app.exceptions import NotFoundError
from app.models.invoice import Invoice
from app.reconciliation.settle import (
    PENDING_ACTION_STATUSES,
    SettleResult,
    SettleSource,
    _revoke_pending_actions,
    settle_invoice,
)

logger = logging.getLogger(__name__)

# Payment methods a merchant can attest to. No card: PAYVRA never touches card data, and a card
# payment would have arrived through Razorpay anyway.
OFFLINE_METHODS = ("neft", "rtgs", "imps", "upi", "cheque", "cash", "other")


def mark_paid_offline(
    db: Session,
    invoice_id: uuid.UUID,
    *,
    merchant_id: uuid.UUID,
    amount_paise: int,
    method: str,
    reference: str | None = None,
    paid_on: date | None = None,
    actor_id: str | None = None,
) -> SettleResult:
    """Record a payment received outside Razorpay and run the standard settle path.

    ``actor`` is ``human``, not ``system``: a person is attesting to this, and the audit log
    should say who. That distinction is what makes the trail defensible when a merchant later
    asks why an invoice closed without a Razorpay payment behind it.
    """
    if method not in OFFLINE_METHODS:
        raise ValueError(f"unknown payment method {method!r}; expected one of {OFFLINE_METHODS}")

    invoice = _load(db, invoice_id, merchant_id)
    settled_at = (
        datetime.combine(paid_on, datetime.min.time(), tzinfo=IST) if paid_on else now_utc()
    )

    return settle_invoice(
        db,
        invoice.id,
        amount_paise,
        source=SettleSource.MANUAL,
        reference=reference,
        paid_on=settled_at,
        actor=ActorType.HUMAN,
        actor_id=actor_id or "merchant",
    )


def mark_disputed(
    db: Session,
    invoice_id: uuid.UUID,
    *,
    merchant_id: uuid.UUID,
    reason: str,
    actor_id: str | None = None,
) -> dict[str, object]:
    """Freeze all outreach on a disputed invoice (api-contracts.md -> Manual actions).

    A dispute is a commercial disagreement, not a collections problem (ADR-008), and CLAUDE.md
    invariant 8 makes it an absolute stop. So this revokes pending actions through the same
    routine the settle path uses — the money has not arrived, but the outreach must stop just as
    completely, and for the same reason: continuing to chase is the thing that does the damage.
    """
    invoice = _load(db, invoice_id, merchant_id)

    revoked = _revoke_pending_actions(db, invoice.id)
    invoice.inferred_cause = UnpaidCause.DISPUTE.value
    invoice.recovery_state = RecoveryState.STOPPED.value
    invoice.stop_reason = StopReason.DISPUTED.value
    db.flush()

    audit_record(
        db,
        merchant_id=merchant_id,
        actor=ActorType.HUMAN,
        actor_id=actor_id or "merchant",
        action_type="invoice.mark_disputed",
        subject_type="invoice",
        subject_id=invoice.id,
        outcome="stopped",
        rationale=f"Marked disputed: {reason}. Revoked {revoked} pending action(s).",
        inputs={"reason": reason, "revoked_actions": revoked},
    )
    logger.info("invoice=%s marked disputed, revoked=%d", invoice.id, revoked)

    return {
        "invoice_id": str(invoice.id),
        "recovery_state": invoice.recovery_state,
        "stop_reason": invoice.stop_reason,
        "inferred_cause": invoice.inferred_cause,
        "revoked_actions": revoked,
    }


def _load(db: Session, invoice_id: uuid.UUID, merchant_id: uuid.UUID) -> Invoice:
    """Scoped to the caller's merchant; a cross-tenant id is a 404, not someone else's invoice."""
    invoice = db.execute(
        select(Invoice).where(Invoice.id == invoice_id, Invoice.merchant_id == merchant_id)
    ).scalar_one_or_none()
    if invoice is None:
        raise NotFoundError(f"invoice {invoice_id} not found")
    return invoice


__all__ = ["OFFLINE_METHODS", "PENDING_ACTION_STATUSES", "mark_disputed", "mark_paid_offline"]
