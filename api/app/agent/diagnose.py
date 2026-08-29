"""Infer *why* an invoice is unpaid, from behavioural signals. Rules first, no LLM.

The mapping is the table in ``architecture/agent-loop.md``, in its documented precedence order.
Only the ``awaiting_docs`` case genuinely needs a model, and it needs a *reply* to read — which is
Phase 7. Until then this module is entirely deterministic, and that is a feature: **an LLM call you
can replace with an `if` statement is a liability, not a feature.**

Diagnosis is a read. It never writes, never contacts anyone, and never decides whether to act — it
only says what it thinks is going on, so the proposal step has something better than "unknown" to
reason from.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.enums import ActionStatus, Channel, PaymentStatus, UnpaidCause
from app.models.action import Action
from app.models.counterparty import Counterparty
from app.models.invoice import Invoice
from app.models.message import Message

#: Touches with nothing back before we suspect the contact rather than the counterparty.
ZERO_ENGAGEMENT_TOUCHES = 3

#: Link opened this many times without paying reads as inability, not oversight.
OPENED_UNPAID_FOR_CASH_CRUNCH = 2

#: Inside this many days past due, a reliable payer's first slip is an oversight, not a signal.
FIRST_SLIP_DPD = 15


@dataclass(frozen=True)
class Signals:
    """The behavioural inputs the mapping reads. Assembled once, per invoice."""

    link_opened_unpaid_count: int
    email_bounced: bool
    zero_engagement: bool
    partial_payment_received: bool
    historically_reliable: bool
    first_slip: bool
    days_past_due: int
    touch_count: int

    def as_audit_payload(self) -> dict[str, object]:
        """What the audit entry records, so a diagnosis can be re-derived later."""
        return {
            "link_opened_unpaid_count": self.link_opened_unpaid_count,
            "email_bounced": self.email_bounced,
            "zero_engagement": self.zero_engagement,
            "partial_payment_received": self.partial_payment_received,
            "historically_reliable": self.historically_reliable,
            "first_slip": self.first_slip,
            "days_past_due": self.days_past_due,
            "touch_count": self.touch_count,
        }


@dataclass(frozen=True)
class Diagnosis:
    cause: UnpaidCause
    confidence: str  # "high" | "medium" | "low"
    rationale: str
    signals: Signals


def _prior_invoice_count(db: Session, invoice: Invoice) -> int:
    """How many other invoices this counterparty has. Used to judge "first slip"."""
    return int(
        db.execute(
            select(func.count())
            .select_from(Invoice)
            .where(
                Invoice.counterparty_id == invoice.counterparty_id,
                Invoice.id != invoice.id,
                Invoice.days_past_due > 0,
            )
        ).scalar_one()
    )


def collect_signals(db: Session, invoice: Invoice, counterparty: Counterparty) -> Signals:
    """Gather the behavioural signals for one invoice. Pure reads."""
    opened_unpaid = int(
        db.execute(
            select(func.count())
            .select_from(Message)
            .join(Action, Action.id == Message.action_id)
            .where(
                Action.invoice_id == invoice.id,
                Message.clicked_at.is_not(None),
            )
        ).scalar_one()
    )

    bounced = bool(
        db.execute(
            select(func.count())
            .select_from(Message)
            .join(Action, Action.id == Message.action_id)
            .where(
                Action.invoice_id == invoice.id,
                Message.channel == Channel.EMAIL.value,
                Message.delivery_status == "bounced",
            )
        ).scalar_one()
    )

    engaged = int(
        db.execute(
            select(func.count())
            .select_from(Message)
            .join(Action, Action.id == Message.action_id)
            .where(
                Action.invoice_id == invoice.id,
                Action.status == ActionStatus.EXECUTED.value,
                (Message.opened_at.is_not(None)) | (Message.clicked_at.is_not(None)),
            )
        ).scalar_one()
    )

    avg_days = counterparty.avg_days_to_pay
    reliable = (
        avg_days is not None
        and int(avg_days) <= invoice.terms_days + 15
        and (counterparty.broken_promise_count or 0) == 0
    )

    return Signals(
        link_opened_unpaid_count=opened_unpaid,
        email_bounced=bounced,
        zero_engagement=invoice.touch_count >= ZERO_ENGAGEMENT_TOUCHES and engaged == 0,
        partial_payment_received=(
            invoice.payment_status == PaymentStatus.PARTIALLY_PAID.value
            or invoice.outstanding_paise < invoice.amount_paise
        ),
        historically_reliable=bool(reliable),
        first_slip=_prior_invoice_count(db, invoice) == 0,
        days_past_due=invoice.days_past_due,
        touch_count=invoice.touch_count,
    )


def infer(signals: Signals) -> tuple[UnpaidCause, str, str]:
    """The mapping table, in precedence order. Returns (cause, confidence, rationale).

    Order matters and is not alphabetical: a bounced email outranks an opened link, because a
    message that never arrived says nothing about willingness to pay.
    """
    if signals.email_bounced:
        return (
            UnpaidCause.WRONG_CONTACT,
            "high",
            "email to this contact bounced, so the invoice may never have been seen",
        )
    if signals.partial_payment_received:
        return (
            UnpaidCause.CASH_CRUNCH,
            "high",
            "a partial payment arrived, which reads as willingness without full liquidity",
        )
    if signals.link_opened_unpaid_count >= OPENED_UNPAID_FOR_CASH_CRUNCH:
        return (
            UnpaidCause.CASH_CRUNCH,
            "high",
            f"payment link opened {signals.link_opened_unpaid_count} times without paying",
        )
    if (
        signals.historically_reliable
        and signals.first_slip
        and signals.days_past_due < FIRST_SLIP_DPD
    ):
        return (
            UnpaidCause.OVERSIGHT,
            "high",
            "a reliable payer's first slip, only days past due",
        )
    if signals.zero_engagement:
        return (
            UnpaidCause.WRONG_CONTACT,
            "medium",
            f"{signals.touch_count} touches with no open, click or reply",
        )
    return (
        UnpaidCause.UNKNOWN,
        "low",
        "no distinguishing signal; treat as a standard reminder",
    )


def diagnose(db: Session, invoice: Invoice, counterparty: Counterparty) -> Diagnosis:
    """Full diagnosis for one invoice.

    A ``dispute`` or ``refusal`` already recorded on the invoice is authoritative and is not
    re-derived: those come from a human or a counterparty saying so outright, and a behavioural
    guess must never overwrite a stated fact.
    """
    signals = collect_signals(db, invoice, counterparty)

    stated = invoice.inferred_cause
    if stated in (UnpaidCause.DISPUTE.value, UnpaidCause.REFUSAL.value):
        return Diagnosis(
            cause=UnpaidCause(stated),
            confidence="high",
            rationale=f"{stated} was recorded on the invoice; not re-derived from signals",
            signals=signals,
        )

    cause, confidence, rationale = infer(signals)
    return Diagnosis(cause=cause, confidence=confidence, rationale=rationale, signals=signals)


def resolve_counterparty(db: Session, invoice: Invoice) -> Counterparty:
    """Load the counterparty an invoice belongs to. Raises if the FK is dangling."""
    counterparty = db.get(Counterparty, invoice.counterparty_id)
    if counterparty is None:  # pragma: no cover - FK guarantees this
        raise LookupError(f"counterparty {invoice.counterparty_id} not found")
    return counterparty


__all__ = ["Diagnosis", "Signals", "collect_signals", "diagnose", "infer", "resolve_counterparty"]
