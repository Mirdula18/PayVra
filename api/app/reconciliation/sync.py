"""Pull settlement state from Razorpay instead of waiting to be pushed it.

**Webhooks get missed.** Twice on this project a real payment landed at Razorpay and never
reached the database, because the tunnel forwarding the webhook was not running at that moment.
The money moved in the world and not in the book, and nothing in the system noticed -- which is
the one failure a receivables product cannot shrug at.

So reconciliation has a second path: ask Razorpay what it knows about a link and apply the
difference. Pull is not a replacement for the webhook -- push is faster and carries a signature --
it is the backstop that makes a missed delivery recoverable instead of permanent.

**It settles the delta, never the total.** Razorpay reports the cumulative ``amount_paid`` on a
link, and some of that may already be recorded from a webhook that did arrive. Applying the total
would double-count the money and inflate the one figure the whole product is judged on, so the
already-recorded amount is subtracted first and only the remainder is settled.

Everything else is the existing path: :func:`~app.reconciliation.settle.settle_invoice` does the
work, so a pulled settlement revokes pending outreach, closes promises and writes its audit entry
through exactly the code a webhook uses. Only ``source`` differs, and it differs on purpose -- a
reader can always tell how the system found out.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import Integer, func, select
from sqlalchemy.orm import Session

from app.agent.metrics import SETTLE_ACTION_TYPE
from app.enums import ActorType
from app.models.audit_log import AuditLog
from app.models.invoice import Invoice
from app.models.payment_link import PaymentLink
from app.razorpay.client import RazorpayClient, RazorpayError
from app.reconciliation.settle import SettleSource, settle_invoice

logger = logging.getLogger(__name__)

#: Razorpay link states that mean money has arrived.
PAID_STATES = ("paid", "partially_paid")


@dataclass(frozen=True)
class SyncResult:
    """What the pull found and what it changed."""

    checked: bool
    link_status: str | None = None
    remote_paid_paise: int = 0
    already_recorded_paise: int = 0
    applied_paise: int = 0
    fully_settled: bool = False
    error: str | None = None

    @property
    def changed(self) -> bool:
        return self.applied_paise > 0


def recorded_paise(db: Session, merchant_id: uuid.UUID, invoice_id: uuid.UUID) -> int:
    """How much of this invoice's payment the audit log already accounts for.

    Read from the audit entries rather than from ``outstanding_paise`` for the same reason
    :mod:`app.agent.metrics` does: that column also moves for a write-off or a correction, and the
    delta calculation has to be able to say exactly what it counted.
    """
    total = db.execute(
        select(
            func.coalesce(
                func.sum(AuditLog.inputs["amount_paise"].astext.cast(Integer)), 0
            )
        ).where(
            AuditLog.merchant_id == merchant_id,
            AuditLog.action_type == SETTLE_ACTION_TYPE,
            AuditLog.subject_id == invoice_id,
        )
    ).scalar_one()
    return int(total or 0)


def sync_link(
    db: Session,
    link: PaymentLink,
    *,
    merchant_id: uuid.UUID,
    client: RazorpayClient | None = None,
) -> SyncResult:
    """Ask Razorpay about one link and settle whatever it reports beyond what we hold.

    Does **not** commit -- the caller owns the transaction, matching ``settle_invoice``. A network
    failure returns a result carrying the error rather than raising: a page that polls for payment
    must not 500 because Razorpay was briefly unreachable.
    """
    rp = client or RazorpayClient()
    try:
        remote = rp.fetch_payment_link(link.razorpay_link_id)
    except RazorpayError as exc:
        logger.warning("link sync failed link=%s: %s", link.razorpay_link_id, exc)
        return SyncResult(checked=False, error=str(exc))

    status = str(remote.get("status") or "")
    remote_paid = int(remote.get("amount_paid") or 0)

    # Keep our copy of the link's own state current even when no money moved, so the UI stops
    # showing "created" for a link Razorpay considers paid.
    if status and status != link.status:
        link.status = status
        db.flush()

    already = recorded_paise(db, merchant_id, link.invoice_id)
    delta = remote_paid - already

    if status not in PAID_STATES or delta <= 0:
        return SyncResult(
            checked=True,
            link_status=status,
            remote_paid_paise=remote_paid,
            already_recorded_paise=already,
        )

    invoice = db.get(Invoice, link.invoice_id)
    if invoice is None or invoice.merchant_id != merchant_id:  # pragma: no cover - FK guards this
        return SyncResult(checked=True, link_status=status, error="invoice not found")

    # Never settle beyond what is outstanding. Razorpay is the authority on what it collected, but
    # this row is the authority on what was owed, and an overpayment must not invent recovery.
    applied = min(delta, int(invoice.outstanding_paise))
    if applied <= 0:
        return SyncResult(
            checked=True,
            link_status=status,
            remote_paid_paise=remote_paid,
            already_recorded_paise=already,
        )

    result = settle_invoice(
        db,
        invoice.id,
        applied,
        source=SettleSource.POLL,
        reference=link.razorpay_link_id,
        actor=ActorType.SYSTEM,
        actor_id="razorpay:poll",
    )
    logger.info(
        "link sync settled invoice=%s applied=%d status=%s", invoice.id, applied, status
    )
    return SyncResult(
        checked=True,
        link_status=status,
        remote_paid_paise=remote_paid,
        already_recorded_paise=already,
        applied_paise=applied,
        fully_settled=result.fully_settled,
    )


def sync_invoice(
    db: Session,
    invoice_id: uuid.UUID,
    merchant_id: uuid.UUID,
    *,
    client: RazorpayClient | None = None,
) -> SyncResult:
    """Sync the most recent link on one invoice. Returns an unchecked result if it has none."""
    link = (
        db.execute(
            select(PaymentLink)
            .where(PaymentLink.invoice_id == invoice_id)
            .order_by(PaymentLink.created_at.desc())
        )
        .scalars()
        .first()
    )
    if link is None:
        return SyncResult(checked=False, error="no payment link on this invoice")
    return sync_link(db, link, merchant_id=merchant_id, client=client)


__all__ = ["PAID_STATES", "SyncResult", "recorded_paise", "sync_invoice", "sync_link"]
