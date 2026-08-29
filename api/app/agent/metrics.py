"""Run-scoped recovery measurement (FR-17). The number the Track 3 bar asks for.

Two figures, both scoped to one ``recovery_run_id``:

* **Causal (headline)** — money received against invoices *this run acted on*. The figure that
  survives "how do you know your agent caused that?"
* **Time-window (context)** — everything received while the run was open, regardless of cause.

**Measured in rupees received, not invoices settled.** Under ADR-006 an invoice above the Razorpay
link ceiling is collected in tranches, so a Rs 14L receivable can have Rs 10L genuinely recovered
while its status is still ``partially_paid``. A settled-invoice-only figure would report that as
zero -- and would make the tranche mechanism *lower* the number it was chosen to raise.

Rupees come from reconciled settlement events, not from ``outstanding_paise`` deltas (FR-17.5).
That column also moves for a write-off or a correction, and a recovery figure has to be able to
say exactly what it counted when someone asks.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Integer, distinct, func, select
from sqlalchemy.orm import Session

from app.models.action import Action
from app.models.audit_log import AuditLog
from app.models.invoice import Invoice
from app.models.recovery_run import RecoveryRun

#: The audit action_type settle writes. Its ``inputs.amount_paise`` is the money that actually
#: moved, which is why this is the source rather than the invoice row.
SETTLE_ACTION_TYPE = "reconcile.settle"

_AMOUNT = func.coalesce(AuditLog.inputs["amount_paise"].astext.cast(Integer), 0)


@dataclass(frozen=True)
class RecoveryFigure:
    """One way of counting what came in."""

    label: str
    rupees_paise: int
    invoices_paid_in_full: int
    invoices_partially_recovered: int

    @property
    def invoices_touched_by_payment(self) -> int:
        return self.invoices_paid_in_full + self.invoices_partially_recovered


@dataclass(frozen=True)
class RunRecovery:
    """Both figures for one run, plus what the run itself did."""

    recovery_run_id: uuid.UUID
    started_at: datetime
    finished_at: datetime | None
    dry_run: bool
    accounts_considered: int
    actions_executed: int
    actions_refused: int
    causal: RecoveryFigure
    time_window: RecoveryFigure

    @property
    def diverges(self) -> bool:
        return self.causal.rupees_paise != self.time_window.rupees_paise


def _settlement_rows(
    db: Session, merchant_id: uuid.UUID, start: datetime, end: datetime | None
) -> list[tuple[uuid.UUID, int]]:
    """(invoice_id, amount_paise) for every settlement recorded in the window."""
    stmt = (
        select(AuditLog.subject_id, _AMOUNT)
        .where(
            AuditLog.merchant_id == merchant_id,
            AuditLog.action_type == SETTLE_ACTION_TYPE,
            AuditLog.created_at >= start,
        )
        .order_by(AuditLog.id)
    )
    if end is not None:
        stmt = stmt.where(AuditLog.created_at <= end)
    return [(row[0], int(row[1] or 0)) for row in db.execute(stmt)]


def _touched_invoice_ids(db: Session, run: RecoveryRun) -> set[uuid.UUID]:
    """Invoices this run acted on with the gate's approval.

    **Keyed on ``gate_failure_reason``, not on ``status``, because status mutates.** An earlier
    version used ``status IN (executed, gated_pass)`` and produced a figure of exactly zero every
    time an invoice was actually paid: settlement revokes the invoice's pending actions (that is
    the most important write in the product), which flips the status to ``revoked`` and erased the
    only evidence that the run had acted. Two correct behaviours cancelling each other out --
    the recovery figure went to zero *because* recovery happened.

    ``gate_failure_reason`` is durable: it is written once, when the verdict is recorded, and no
    later event rewrites it. Null means the gate approved the action; a refused one always carries
    its reason.

    A refused action is deliberately excluded. If the gate declined to contact someone and they
    paid anyway, that money is not ours to claim; counting it would make the causal figure exactly
    as unfalsifiable as the time-window one it exists to improve on.

    A dry run acted on nobody -- it created no link and contacted no one -- so it claims nothing,
    however much money happens to arrive while it is running.
    """
    if run.dry_run:
        return set()

    return {
        row[0]
        for row in db.execute(
            select(distinct(Action.invoice_id)).where(
                Action.recovery_run_id == run.id,
                Action.gate_failure_reason.is_(None),
            )
        )
    }


def _summarise(
    db: Session, label: str, rows: list[tuple[uuid.UUID, int]]
) -> RecoveryFigure:
    """Total the money and classify each invoice as fully or partially recovered."""
    total = sum(amount for _, amount in rows)
    invoice_ids = {invoice_id for invoice_id, _ in rows}
    if not invoice_ids:
        return RecoveryFigure(label, 0, 0, 0)

    outstanding: dict[uuid.UUID, int] = {
        row[0]: int(row[1] or 0)
        for row in db.execute(
            select(Invoice.id, Invoice.outstanding_paise).where(Invoice.id.in_(invoice_ids))
        )
    }
    # Absent from the map means the invoice is gone; treat as still owing rather than as settled,
    # so a missing row can never inflate the "paid in full" count.
    full = len([i for i in invoice_ids if outstanding.get(i, 1) <= 0])
    return RecoveryFigure(label, total, full, len(invoice_ids) - full)


def recovery_for_run(db: Session, recovery_run_id: uuid.UUID) -> RunRecovery:
    """Both recovery figures for one run. **The two use different time bounds, deliberately.**

    *Time-window* closes at ``finished_at``: it answers "what arrived while the run was open?",
    which is only meaningful for the moments the run actually spanned. It stays open while a run
    is still going, so a mid-run report is not silently truncated.

    *Causal* has *no upper bound*. It answers "what came in against invoices this run acted on?",
    and recovery is not instant -- a run completes in seconds and a counterparty pays hours or days
    later. Bounding causal by ``finished_at`` made it structurally zero: it could only ever count
    money that arrived inside the few seconds the run was executing, which is nobody's payment.
    That is a snapshot that grows after the run ends, exactly as the FR-17 divergence table says.

    Both start at ``started_at``. Money that arrived before the run began is not the run's, under
    either reading.
    """
    run = db.get(RecoveryRun, recovery_run_id)
    if run is None:
        raise LookupError(f"recovery run {recovery_run_id} not found")

    touched = _touched_invoice_ids(db, run)

    # Unbounded above: everything received against this merchant since the run started.
    causal_rows = [
        (invoice_id, amount)
        for invoice_id, amount in _settlement_rows(db, run.merchant_id, run.started_at, None)
        if invoice_id in touched
    ]
    window_rows = _settlement_rows(db, run.merchant_id, run.started_at, run.finished_at)

    return RunRecovery(
        recovery_run_id=run.id,
        started_at=run.started_at,
        finished_at=run.finished_at,
        dry_run=run.dry_run,
        accounts_considered=run.accounts_considered,
        actions_executed=run.actions_executed,
        actions_refused=run.actions_refused,
        causal=_summarise(db, "causal", causal_rows),
        time_window=_summarise(db, "time_window", window_rows),
    )


def rupees(paise: int) -> str:
    """Paise as a plain rupee figure for a report line."""
    from app.money import paise_to_exact

    return paise_to_exact(paise)


__all__ = [
    "SETTLE_ACTION_TYPE",
    "RecoveryFigure",
    "RunRecovery",
    "recovery_for_run",
    "rupees",
]
