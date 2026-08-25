"""Manual invoice actions (api-contracts.md -> Manual actions).

Both endpoints run the *identical* code the automated paths use: ``mark-paid-offline`` calls the
same ``settle_invoice`` a webhook does, and ``mark-disputed`` revokes through the same routine. A
second implementation for the human path is a second implementation to keep correct.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter
from sqlalchemy import desc, select

from app.deps import DbSession, MerchantId
from app.enums import PaymentStatus
from app.exceptions import NotFoundError, ValidationError
from app.models.audit_log import AuditLog
from app.models.invoice import Invoice
from app.reconciliation import manual
from app.reconciliation.settle import cancel_links_after_settle
from app.schemas.reconciliation import (
    MarkDisputedRequest,
    MarkDisputedResponse,
    MarkPaidOfflineRequest,
    ReconciliationStatusResponse,
    SettleResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/invoices", tags=["reconciliation"])


@router.post("/{invoice_id}/mark-paid-offline", response_model=SettleResponse)
def mark_paid_offline(
    db: DbSession,
    merchant_id: MerchantId,
    invoice_id: uuid.UUID,
    body: MarkPaidOfflineRequest,
) -> SettleResponse:
    """Record a cheque/NEFT/RTGS payment and run the standard settle path (FR-13.6).

    Runs the identical settle as a webhook: revokes every pending action, closes open promises,
    writes the audit entry with ``actor: human``. The response leads with ``revoked_actions``
    because that is what the merchant actually wants confirmed — that nothing further will be
    sent to someone who has already paid.
    """
    try:
        result = manual.mark_paid_offline(
            db,
            invoice_id,
            merchant_id=merchant_id,
            amount_paise=body.amount_paise,
            method=body.method,
            reference=body.reference,
            paid_on=body.paid_on,
        )
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc

    db.commit()

    # Outside the settle transaction, after the commit -- an outbound HTTP call must never be
    # able to roll back a revocation. See reconciliation/settle.py.
    cancelled = 0
    if result.fully_settled:
        cancelled, errors = cancel_links_after_settle(db, invoice_id)
        db.commit()
        if errors:
            logger.info("left %d link(s) for link_hygiene on invoice=%s", len(errors), invoice_id)

    return SettleResponse(**result.as_dict(), links_cancelled=cancelled)


@router.post("/{invoice_id}/mark-disputed", response_model=MarkDisputedResponse)
def mark_disputed(
    db: DbSession,
    merchant_id: MerchantId,
    invoice_id: uuid.UUID,
    body: MarkDisputedRequest,
) -> MarkDisputedResponse:
    """Freeze all outreach immediately. A dispute is not a collections problem (ADR-008)."""
    result = manual.mark_disputed(db, invoice_id, merchant_id=merchant_id, reason=body.reason)
    db.commit()
    return MarkDisputedResponse(**result)  # type: ignore[arg-type]


@router.get("/{invoice_id}/reconciliation-status", response_model=ReconciliationStatusResponse)
def reconciliation_status(
    db: DbSession, merchant_id: MerchantId, invoice_id: uuid.UUID
) -> ReconciliationStatusResponse:
    """What the dashboard polls after a payment lands (prompts/demo-script.md; Phase 8 frontend).

    A read, and deliberately a cheap one — it is called on a short interval while a demo is on
    screen. Two indexed lookups, no aggregation over actions.

    ``revoked_actions`` comes from the ``reconcile.settle`` audit entry rather than being
    recounted from ``actions``. Recounting would return every action ever revoked on this invoice,
    including ones cancelled for unrelated reasons like a dispute; the audit entry records what
    *this settlement* did, which is the number the trail will show a judge.
    """
    invoice = db.execute(
        select(Invoice).where(Invoice.id == invoice_id, Invoice.merchant_id == merchant_id)
    ).scalar_one_or_none()
    if invoice is None:
        raise NotFoundError(f"invoice {invoice_id} not found")

    entry = db.execute(
        select(AuditLog)
        .where(
            AuditLog.merchant_id == merchant_id,
            AuditLog.subject_id == invoice_id,
            AuditLog.action_type == "reconcile.settle",
        )
        .order_by(desc(AuditLog.id))
        .limit(1)
    ).scalar_one_or_none()

    inputs = entry.inputs if entry is not None else {}
    return ReconciliationStatusResponse(
        invoice_id=invoice.id,
        settled=invoice.payment_status == PaymentStatus.PAID.value,
        settled_at=invoice.settled_at,
        revoked_actions=_count(inputs, "revoked_actions"),
        promises_closed=_count(inputs, "promises_closed"),
        payment_status=invoice.payment_status,
        outstanding_paise=invoice.outstanding_paise,
    )


def _count(inputs: dict[str, Any], key: str) -> int:
    """Read an integer out of a JSONB payload without trusting its type.

    ``audit_log.inputs`` is JSONB written by several call sites over time; a value that is not
    a number should render as 0 on a dashboard rather than 500 the poll.
    """
    value = inputs.get(key)
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
