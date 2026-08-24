"""Worklist endpoints (architecture/api-contracts.md -> Worklist).

``GET /worklist`` is the hot path and the first screen a merchant sees. Two things govern how it
is written:

* **It must hit ``idx_worklist``** ``(merchant_id, recovery_state, priority_score DESC)``.
  That index only orders rows once ``recovery_state`` is pinned by equality, so the ranked query
  fixes it to one state per scan and never sorts across states in SQL.
* **Pinned rows are fetched separately** rather than with ``ORDER BY is_pinned DESC,
  priority_score DESC``. That ordering would defeat the index for every row in order to float a
  handful, so the pinned set (small, merchant-curated) is a second small query merged in Python.

Ranking itself is precomputed by ``scoring.worklist.rescore`` and read from
``invoices.priority_score``. Nothing is scored inside a request, and no LLM is involved anywhere
in this path.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Query
from sqlalchemy import ColumnElement, Select, and_, func, or_, select
from sqlalchemy.orm import Session

from app.clock import today
from app.deps import DbSession, MerchantId
from app.enums import RecoveryState, StopReason
from app.exceptions import NotFoundError, ValidationError
from app.models.counterparty import Counterparty
from app.models.invoice import Invoice
from app.schemas.worklist import (
    CounterpartyRef,
    SnoozeRequest,
    WorklistActionResult,
    WorklistItem,
    WorklistResponse,
    WorklistSummary,
)

router = APIRouter(prefix="/worklist", tags=["worklist"])

# States a row can be in and still be worth a merchant's attention. Settled and stopped are
# terminal: an excluded or settled invoice must never reappear on the worklist.
ACTIVE_STATES: tuple[str, ...] = (
    RecoveryState.NOT_STARTED.value,
    RecoveryState.NUDGED.value,
    RecoveryState.CHASING.value,
    RecoveryState.PROMISED.value,
    RecoveryState.BROKEN_PROMISE.value,
    RecoveryState.ESCALATED.value,
    RecoveryState.HUMAN_REVIEW.value,
)

# A row is "high risk" when the model thinks it is more likely than not to go unrecovered.
HIGH_RISK_BELOW = 0.5


def _visible(merchant_id: uuid.UUID, as_of: date) -> list[ColumnElement[bool]]:
    """Predicates every worklist read shares: this merchant, not snoozed."""
    return [
        Invoice.merchant_id == merchant_id,
        or_(Invoice.snoozed_until.is_(None), Invoice.snoozed_until <= as_of),
    ]


def _ranked_query(merchant_id: uuid.UUID, state: str, as_of: date) -> Select:
    """One state, ordered by priority -- the shape ``idx_worklist`` can serve without a sort."""
    return (
        select(Invoice)
        .where(
            *_visible(merchant_id, as_of),
            Invoice.recovery_state == state,
            Invoice.is_pinned.is_(False),
        )
        .order_by(Invoice.priority_score.desc())
    )


def _to_item(invoice: Invoice, counterparty: Counterparty) -> WorklistItem:
    return WorklistItem(
        invoice_id=invoice.id,
        invoice_number=invoice.invoice_number,
        counterparty=CounterpartyRef(id=counterparty.id, name=counterparty.name),
        outstanding_paise=invoice.outstanding_paise,
        days_past_due=invoice.days_past_due,
        aging_bucket=invoice.aging_bucket,
        crosses_msme_45=invoice.crosses_msme_45,
        recovery_state=invoice.recovery_state,
        inferred_cause=invoice.inferred_cause,
        # Defaults cover an invoice imported since the last rescore. The nightly job fills them,
        # and priority_reason is never left null on a row the merchant can see.
        collectability_score=(
            invoice.collectability_score if invoice.collectability_score is not None else Decimal(0)
        ),
        priority_score=(
            invoice.priority_score if invoice.priority_score is not None else Decimal(0)
        ),
        priority_reason=invoice.priority_reason
        or "Not yet scored; the nightly rescore is pending.",
        current_tone_tier=invoice.current_tone_tier,
        touch_count=invoice.touch_count,
        is_pinned=invoice.is_pinned,
        snoozed_until=invoice.snoozed_until,
    )


def _load_counterparties(db: Session, invoices: list[Invoice]) -> dict[uuid.UUID, Counterparty]:
    if not invoices:
        return {}
    ids = {invoice.counterparty_id for invoice in invoices}
    return {
        cp.id: cp
        for cp in db.execute(select(Counterparty).where(Counterparty.id.in_(ids))).scalars()
    }


@router.get("", response_model=WorklistResponse)
def get_worklist(
    db: DbSession,
    merchant_id: MerchantId,
    state: str | None = Query(default=None, description="Filter to one recovery_state."),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> WorklistResponse:
    """The primary screen. Ranked by priority_score, never alphabetical and never by age.

    Pinned rows lead, in priority order among themselves, then the ranked remainder.
    """
    as_of = today()

    if state is not None and state not in ACTIVE_STATES:
        raise ValidationError(
            f"unknown or terminal state {state!r}; expected one of {', '.join(ACTIVE_STATES)}"
        )
    states = (state,) if state else ACTIVE_STATES

    # Pinned first. Small, merchant-curated set, so a separate query rather than an ORDER BY that
    # would cost the index for every other row.
    pinned = list(
        db.execute(
            select(Invoice)
            .where(
                *_visible(merchant_id, as_of),
                Invoice.is_pinned.is_(True),
                Invoice.recovery_state.in_(states),
            )
            .order_by(Invoice.priority_score.desc().nullslast())
        )
        .scalars()
        .all()
    )

    # One index-friendly scan per state, merged by priority. With a single state this is exactly
    # the query idx_worklist serves; with several it is one such scan each, still index-driven.
    ranked: list[Invoice] = []
    for one_state in states:
        ranked.extend(
            db.execute(_ranked_query(merchant_id, one_state, as_of).limit(limit + offset))
            .scalars()
            .all()
        )
    ranked.sort(key=lambda i: i.priority_score if i.priority_score is not None else 0, reverse=True)

    ordered = pinned + ranked
    page = ordered[offset : offset + limit]
    counterparties = _load_counterparties(db, page)

    base = and_(*_visible(merchant_id, as_of), Invoice.recovery_state.in_(states))
    total = db.execute(select(func.count()).select_from(Invoice).where(base)).scalar_one()
    totals = db.execute(
        select(
            func.coalesce(func.sum(Invoice.outstanding_paise), 0),
            func.count().filter(Invoice.days_past_due > 0),
            func.count().filter(Invoice.collectability_score < HIGH_RISK_BELOW),
        ).where(base)
    ).one()

    return WorklistResponse(
        items=[_to_item(inv, counterparties[inv.counterparty_id]) for inv in page],
        total=total,
        summary=WorklistSummary(
            total_outstanding_paise=int(totals[0]),
            overdue_count=int(totals[1]),
            high_risk_count=int(totals[2]),
        ),
        limit=limit,
        offset=offset,
    )


def _load_invoice(db: Session, merchant_id: uuid.UUID, invoice_id: uuid.UUID) -> Invoice:
    """Scoped to the caller's merchant; a cross-tenant id is a 404, not someone else's invoice."""
    invoice = db.execute(
        select(Invoice).where(Invoice.id == invoice_id, Invoice.merchant_id == merchant_id)
    ).scalar_one_or_none()
    if invoice is None:
        raise NotFoundError(f"invoice {invoice_id} not found")
    return invoice


def _result(invoice: Invoice) -> WorklistActionResult:
    return WorklistActionResult(
        invoice_id=invoice.id,
        is_pinned=invoice.is_pinned,
        snoozed_until=invoice.snoozed_until,
        recovery_state=invoice.recovery_state,
        stop_reason=invoice.stop_reason,
    )


@router.post("/{invoice_id}/pin", response_model=WorklistActionResult)
def pin(db: DbSession, merchant_id: MerchantId, invoice_id: uuid.UUID) -> WorklistActionResult:
    """Float an invoice to the top of the worklist regardless of its score (FR-4.5).

    Pinning also lifts a snooze -- asking for something to lead the list while it is hidden is a
    contradiction, and the pin is the later instruction.
    """
    invoice = _load_invoice(db, merchant_id, invoice_id)
    invoice.is_pinned = True
    invoice.snoozed_until = None
    db.commit()
    return _result(invoice)


@router.post("/{invoice_id}/snooze", response_model=WorklistActionResult)
def snooze(
    db: DbSession, merchant_id: MerchantId, invoice_id: uuid.UUID, body: SnoozeRequest
) -> WorklistActionResult:
    """Hide an invoice from the worklist until a date (FR-4.5).

    A snooze suppresses attention, not recovery state: the invoice keeps its history and returns
    on the given IST business day exactly as it was.
    """
    invoice = _load_invoice(db, merchant_id, invoice_id)
    if body.until <= today():
        raise ValidationError("snooze 'until' must be a future date")
    invoice.snoozed_until = body.until
    invoice.is_pinned = False
    db.commit()
    return _result(invoice)


@router.post("/{invoice_id}/exclude", response_model=WorklistActionResult)
def exclude(db: DbSession, merchant_id: MerchantId, invoice_id: uuid.UUID) -> WorklistActionResult:
    """Remove an invoice from automation entirely (FR-4.5).

    Terminal, per CLAUDE.md invariant 8: stopping rules are absolute. This is expressed as
    ``recovery_state='stopped'`` with ``stop_reason='merchant_excluded'`` rather than a new
    column, so every existing stop check already honours it and nothing has to learn a new way
    for an invoice to be out of scope.
    """
    invoice = _load_invoice(db, merchant_id, invoice_id)
    invoice.recovery_state = RecoveryState.STOPPED.value
    invoice.stop_reason = StopReason.MERCHANT_EXCLUDED.value
    invoice.is_pinned = False
    db.commit()
    return _result(invoice)
