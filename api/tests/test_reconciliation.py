"""Reconciliation: the settle path, atomicity, partial payments, and the manual route.

``settle_invoice`` is the most important code in the product. An invoice that settles must have
every pending action revoked **in the same transaction** as the status change — miss it and
PAYVRA messages a customer who paid three hours ago.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.enums import ActionStatus, ActionType, PaymentStatus, RecoveryState, StopReason
from app.models.action import Action
from app.models.invoice import Invoice
from app.models.promise import Promise
from app.reconciliation import manual
from app.reconciliation.settle import SettleSource, settle_invoice

pytestmark = pytest.mark.usefixtures("db_available")


def add_action(
    db: Session, invoice: Invoice, status: str, *, action_type: str = ActionType.SEND_MESSAGE.value
) -> Action:
    action = Action(
        id=uuid.uuid4(),
        merchant_id=invoice.merchant_id,
        invoice_id=invoice.id,
        type=action_type,
        status=status,
        proposed_by="agent",
        rationale="test",
        scheduled_for=datetime.now(UTC) + timedelta(hours=1),
    )
    db.add(action)
    db.flush()
    return action


def add_promise(db: Session, invoice: Invoice, status: str = "open") -> Promise:
    from app.models.reply import Reply

    reply = Reply(
        id=uuid.uuid4(),
        invoice_id=invoice.id,
        counterparty_id=invoice.counterparty_id,
        channel="email",
        raw_text="will pay next week",
        intent="promise_to_pay",
        confidence=0.9,
        received_at=datetime.now(UTC),
    )
    db.add(reply)
    db.flush()
    promise = Promise(
        id=uuid.uuid4(),
        invoice_id=invoice.id,
        reply_id=reply.id,
        promised_date=date(2026, 9, 1),
        confidence=0.9,
        status=status,
    )
    db.add(promise)
    db.flush()
    return promise


# --- THE critical step ------------------------------------------------------------------------


def test_settle_revokes_every_pending_action(
    db_session: Session, gate_invoice: Invoice, gate_merchant: Any
) -> None:
    """The single most important line in the product."""
    pending = [
        add_action(db_session, gate_invoice, ActionStatus.PROPOSED.value),
        add_action(db_session, gate_invoice, ActionStatus.GATED_PASS.value),
        add_action(db_session, gate_invoice, ActionStatus.AWAITING_APPROVAL.value),
    ]
    already_done = add_action(db_session, gate_invoice, ActionStatus.EXECUTED.value)

    result = settle_invoice(
        db_session, gate_invoice.id, gate_invoice.outstanding_paise, source=SettleSource.WEBHOOK
    )

    assert result.revoked_actions == 3
    db_session.expire_all()
    for action in pending:
        row = db_session.get(Action, action.id)
        assert row.status == ActionStatus.REVOKED.value
        assert row.revoked_at is not None
    # An action that already happened is history, not pending work.
    assert db_session.get(Action, already_done.id).status == ActionStatus.EXECUTED.value


def test_settle_and_revoke_are_one_transaction(
    db_session: Session, gate_invoice: Invoice, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If revocation fails, the status update must not survive.

    The rollback case, asserted rather than assumed: an invoice marked paid with live outreach
    behind it is precisely the failure the atomicity claim exists to prevent.
    """
    from app.reconciliation import settle as settle_mod

    add_action(db_session, gate_invoice, ActionStatus.PROPOSED.value)
    invoice_id = gate_invoice.id
    original_status = gate_invoice.payment_status
    original_outstanding = gate_invoice.outstanding_paise
    db_session.commit()

    def exploding_revoke(*args: object, **kwargs: object) -> int:
        raise RuntimeError("revocation failed")

    monkeypatch.setattr(settle_mod, "_revoke_pending_actions", exploding_revoke)

    with pytest.raises(RuntimeError, match="revocation failed"):
        settle_invoice(db_session, invoice_id, original_outstanding, source=SettleSource.WEBHOOK)
    db_session.rollback()

    db_session.expire_all()
    invoice = db_session.get(Invoice, invoice_id)
    assert invoice.payment_status == original_status, "status change survived a failed revoke"
    assert invoice.outstanding_paise == original_outstanding
    assert invoice.settled_at is None


def test_settle_closes_open_promises(db_session: Session, gate_invoice: Invoice) -> None:
    promise = add_promise(db_session, gate_invoice)
    result = settle_invoice(
        db_session, gate_invoice.id, gate_invoice.outstanding_paise, source=SettleSource.WEBHOOK
    )
    assert result.promises_closed == 1
    db_session.expire_all()
    assert db_session.get(Promise, promise.id).status == "kept"


def test_settle_marks_the_invoice_settled(db_session: Session, gate_invoice: Invoice) -> None:
    result = settle_invoice(
        db_session, gate_invoice.id, gate_invoice.outstanding_paise, source=SettleSource.WEBHOOK
    )
    assert result.fully_settled
    assert result.outstanding_after_paise == 0
    db_session.expire_all()
    invoice = db_session.get(Invoice, gate_invoice.id)
    assert invoice.payment_status == PaymentStatus.PAID.value
    assert invoice.recovery_state == RecoveryState.SETTLED.value
    assert invoice.stop_reason == StopReason.SETTLED.value
    assert invoice.settled_at is not None


def test_settle_writes_an_executed_audit_entry(
    db_session: Session, gate_invoice: Invoice, gate_merchant: Any
) -> None:
    """The first 'executed' outcome in the codebase: unlike a gate verdict, money actually moved."""
    add_action(db_session, gate_invoice, ActionStatus.PROPOSED.value)
    settle_invoice(
        db_session, gate_invoice.id, gate_invoice.outstanding_paise, source=SettleSource.WEBHOOK
    )
    row = db_session.execute(
        text(
            "SELECT outcome, rationale, inputs FROM audit_log "
            "WHERE merchant_id = :m AND action_type = 'reconcile.settle' ORDER BY id DESC LIMIT 1"
        ),
        {"m": gate_merchant.id},
    ).one()
    outcome, rationale, inputs = row
    assert outcome == "executed"
    assert "Revoked 1 pending action" in rationale
    assert inputs["revoked_actions"] == 1
    assert inputs["source"] == "webhook"


# --- partial payments -------------------------------------------------------------------------


def test_a_partial_payment_reduces_outstanding_and_lowers_tone(
    db_session: Session, gate_invoice: Invoice
) -> None:
    """FR-13.4: they are paying. The right response to good faith is not a firmer letter."""
    gate_invoice.current_tone_tier = 3
    db_session.flush()
    half = gate_invoice.outstanding_paise // 2

    result = settle_invoice(db_session, gate_invoice.id, half, source=SettleSource.WEBHOOK)

    assert not result.fully_settled
    assert result.outstanding_after_paise == gate_invoice.amount_paise - half
    assert result.tone_tier_before == 3
    assert result.tone_tier_after == 2
    db_session.expire_all()
    invoice = db_session.get(Invoice, gate_invoice.id)
    assert invoice.payment_status == PaymentStatus.PARTIALLY_PAID.value
    assert invoice.inferred_cause == "cash_crunch"


def test_tone_never_drops_below_tier_one(db_session: Session, gate_invoice: Invoice) -> None:
    gate_invoice.current_tone_tier = 1
    db_session.flush()
    result = settle_invoice(db_session, gate_invoice.id, 1000, source=SettleSource.WEBHOOK)
    assert result.tone_tier_after == 1


def test_a_partial_payment_still_revokes_pending_actions(
    db_session: Session, gate_invoice: Invoice
) -> None:
    """The queued message quotes the old amount, so it must not go out either."""
    add_action(db_session, gate_invoice, ActionStatus.GATED_PASS.value)
    result = settle_invoice(db_session, gate_invoice.id, 1000, source=SettleSource.WEBHOOK)
    assert result.revoked_actions == 1


def test_a_partial_payment_supersedes_rather_than_keeps_a_promise(
    db_session: Session, gate_invoice: Invoice
) -> None:
    promise = add_promise(db_session, gate_invoice)
    settle_invoice(db_session, gate_invoice.id, 1000, source=SettleSource.WEBHOOK)
    db_session.expire_all()
    assert db_session.get(Promise, promise.id).status == "superseded"


# --- idempotency and edge cases -----------------------------------------------------------------


def test_settling_an_already_settled_invoice_is_a_no_op(
    db_session: Session, gate_invoice: Invoice
) -> None:
    """A redelivered webhook must not drive outstanding negative."""
    settle_invoice(
        db_session, gate_invoice.id, gate_invoice.outstanding_paise, source=SettleSource.WEBHOOK
    )
    again = settle_invoice(db_session, gate_invoice.id, 500_000, source=SettleSource.WEBHOOK)

    assert again.already_settled
    assert again.amount_applied_paise == 0
    db_session.expire_all()
    assert db_session.get(Invoice, gate_invoice.id).outstanding_paise == 0


def test_an_overpayment_does_not_go_negative(db_session: Session, gate_invoice: Invoice) -> None:
    result = settle_invoice(
        db_session,
        gate_invoice.id,
        gate_invoice.outstanding_paise * 3,
        source=SettleSource.WEBHOOK,
    )
    assert result.amount_applied_paise == gate_invoice.amount_paise
    assert result.outstanding_after_paise == 0


def test_a_zero_or_negative_amount_is_rejected(db_session: Session, gate_invoice: Invoice) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        settle_invoice(db_session, gate_invoice.id, 0, source=SettleSource.WEBHOOK)


# --- the manual path --------------------------------------------------------------------------


def test_mark_paid_offline_uses_the_identical_settle_path(
    db_session: Session, gate_invoice: Invoice, gate_merchant: Any
) -> None:
    add_action(db_session, gate_invoice, ActionStatus.PROPOSED.value)
    result = manual.mark_paid_offline(
        db_session,
        gate_invoice.id,
        merchant_id=gate_merchant.id,
        amount_paise=gate_invoice.outstanding_paise,
        method="neft",
        reference="UTR123456",
    )
    assert result.source == "manual"
    assert result.revoked_actions == 1
    assert result.fully_settled


def test_mark_paid_offline_records_a_human_actor(
    db_session: Session, gate_invoice: Invoice, gate_merchant: Any
) -> None:
    """A person is attesting to this, and the audit trail should say so."""
    manual.mark_paid_offline(
        db_session,
        gate_invoice.id,
        merchant_id=gate_merchant.id,
        amount_paise=gate_invoice.outstanding_paise,
        method="cheque",
        reference="CHQ-99",
    )
    actor = db_session.execute(
        text(
            "SELECT actor FROM audit_log WHERE merchant_id = :m "
            "AND action_type = 'reconcile.settle' ORDER BY id DESC LIMIT 1"
        ),
        {"m": gate_merchant.id},
    ).scalar_one()
    assert actor == "human"


def test_an_unknown_payment_method_is_rejected(
    db_session: Session, gate_invoice: Invoice, gate_merchant: Any
) -> None:
    with pytest.raises(ValueError, match="unknown payment method"):
        manual.mark_paid_offline(
            db_session,
            gate_invoice.id,
            merchant_id=gate_merchant.id,
            amount_paise=1000,
            method="bitcoin",
        )


def test_mark_paid_offline_is_tenant_scoped(db_session: Session, gate_invoice: Invoice) -> None:
    from app.exceptions import NotFoundError

    with pytest.raises(NotFoundError):
        manual.mark_paid_offline(
            db_session,
            gate_invoice.id,
            merchant_id=uuid.uuid4(),
            amount_paise=1000,
            method="neft",
        )


def test_mark_disputed_freezes_outreach(
    db_session: Session, gate_invoice: Invoice, gate_merchant: Any
) -> None:
    """A dispute is a commercial disagreement, not a collections problem (ADR-008)."""
    add_action(db_session, gate_invoice, ActionStatus.PROPOSED.value)
    add_action(db_session, gate_invoice, ActionStatus.AWAITING_APPROVAL.value)

    result = manual.mark_disputed(
        db_session,
        gate_invoice.id,
        merchant_id=gate_merchant.id,
        reason="Customer claims short delivery on PO-8821",
    )

    assert result["revoked_actions"] == 2
    assert result["recovery_state"] == RecoveryState.STOPPED.value
    assert result["stop_reason"] == StopReason.DISPUTED.value
    assert result["inferred_cause"] == "dispute"


# --- the two paths produce identical state ------------------------------------------------------


def _state(db: Session, invoice_id: uuid.UUID) -> dict[str, Any]:
    """Everything a settle is supposed to change, for a like-for-like comparison."""
    db.expire_all()
    invoice = db.get(Invoice, invoice_id)
    actions = (
        db.execute(
            text("SELECT status FROM actions WHERE invoice_id = :i ORDER BY status"),
            {"i": invoice_id},
        )
        .scalars()
        .all()
    )
    promises = (
        db.execute(
            text("SELECT status FROM promises WHERE invoice_id = :i ORDER BY status"),
            {"i": invoice_id},
        )
        .scalars()
        .all()
    )
    return {
        "payment_status": invoice.payment_status,
        "recovery_state": invoice.recovery_state,
        "stop_reason": invoice.stop_reason,
        "outstanding_paise": invoice.outstanding_paise,
        "current_tone_tier": invoice.current_tone_tier,
        "actions": list(actions),
        "promises": list(promises),
    }


def test_the_webhook_and_manual_paths_produce_identical_state(
    db_session: Session, gate_merchant: Any, gate_counterparty: Any
) -> None:
    """One settle path, not two. If these ever diverge, one of them is untested in production."""

    def build(number: str) -> Invoice:
        invoice = Invoice(
            id=uuid.uuid4(),
            merchant_id=gate_merchant.id,
            counterparty_id=gate_counterparty.id,
            invoice_number=number,
            amount_paise=500_000,
            outstanding_paise=500_000,
            issue_date=date(2026, 6, 1),
            due_date=date(2026, 7, 1),
            terms_days=30,
            payment_status=PaymentStatus.UNPAID.value,
            recovery_state=RecoveryState.CHASING.value,
            current_tone_tier=2,
        )
        db_session.add(invoice)
        db_session.flush()
        add_action(db_session, invoice, ActionStatus.PROPOSED.value)
        add_action(db_session, invoice, ActionStatus.GATED_PASS.value)
        add_promise(db_session, invoice)
        return invoice

    via_webhook = build("INV-CMP-WEBHOOK")
    via_manual = build("INV-CMP-MANUAL")

    settle_invoice(db_session, via_webhook.id, 500_000, source=SettleSource.WEBHOOK)
    manual.mark_paid_offline(
        db_session,
        via_manual.id,
        merchant_id=gate_merchant.id,
        amount_paise=500_000,
        method="neft",
        reference="UTR-1",
    )

    assert _state(db_session, via_webhook.id) == _state(db_session, via_manual.id)


# --- the polling endpoint the dashboard uses ----------------------------------------------------


def test_reconciliation_status_reports_the_revoked_count(
    client: Any, api_merchant: uuid.UUID
) -> None:
    """The demo's central number, reachable by polling because the webhook 200 cannot carry it."""
    from app.db import SessionLocal
    from app.models.counterparty import Counterparty

    session = SessionLocal()
    try:
        cp = Counterparty(
            id=uuid.uuid4(),
            merchant_id=api_merchant,
            name="Poll Co",
            name_normalized="poll co",
        )
        session.add(cp)
        session.flush()
        invoice = Invoice(
            id=uuid.uuid4(),
            merchant_id=api_merchant,
            counterparty_id=cp.id,
            invoice_number="INV-POLL-001",
            amount_paise=300_000,
            outstanding_paise=300_000,
            issue_date=date(2026, 6, 1),
            due_date=date(2026, 7, 1),
            terms_days=30,
            payment_status=PaymentStatus.UNPAID.value,
            recovery_state=RecoveryState.CHASING.value,
        )
        session.add(invoice)
        session.flush()
        add_action(session, invoice, ActionStatus.PROPOSED.value)
        add_action(session, invoice, ActionStatus.GATED_PASS.value)
        session.commit()
        invoice_id = invoice.id
    finally:
        session.close()

    headers = {"Authorization": f"Bearer {api_merchant}"}
    path = f"/api/v1/invoices/{invoice_id}/reconciliation-status"

    # Before the payment: nothing settled, nothing revoked.
    before = client.get(path, headers=headers).json()
    assert before["settled"] is False
    assert before["settled_at"] is None
    assert before["revoked_actions"] == 0
    assert before["outstanding_paise"] == 300_000

    client.post(
        f"/api/v1/invoices/{invoice_id}/mark-paid-offline",
        headers=headers,
        json={"amount_paise": 300_000, "method": "neft", "reference": "UTR-POLL"},
    )

    after = client.get(path, headers=headers).json()
    assert after["settled"] is True
    assert after["settled_at"] is not None
    assert after["revoked_actions"] == 2
    assert after["payment_status"] == "paid"
    assert after["outstanding_paise"] == 0


def test_reconciliation_status_is_tenant_scoped(client: Any, api_merchant: uuid.UUID) -> None:
    response = client.get(
        f"/api/v1/invoices/{uuid.uuid4()}/reconciliation-status",
        headers={"Authorization": f"Bearer {api_merchant}"},
    )
    assert response.status_code == 404


def test_reconciliation_status_requires_auth(client: Any) -> None:
    response = client.get(f"/api/v1/invoices/{uuid.uuid4()}/reconciliation-status")
    assert response.status_code == 401


def test_reconciliation_status_reads_the_settle_entry_not_a_recount(
    db_session: Session, gate_invoice: Invoice, gate_merchant: Any
) -> None:
    """An action revoked for an unrelated reason must not inflate the settlement's count.

    Recounting `actions WHERE status='revoked'` would include the dispute revocation below and
    report 2, which is not what this settlement did.
    """
    unrelated = add_action(db_session, gate_invoice, ActionStatus.PROPOSED.value)
    from app.reconciliation.settle import _revoke_pending_actions

    _revoke_pending_actions(db_session, gate_invoice.id)  # revoked by something else entirely
    db_session.flush()

    add_action(db_session, gate_invoice, ActionStatus.GATED_PASS.value)
    result = settle_invoice(
        db_session, gate_invoice.id, gate_invoice.outstanding_paise, source=SettleSource.WEBHOOK
    )

    total_revoked = db_session.execute(
        text("SELECT count(*) FROM actions WHERE invoice_id = :i AND status = 'revoked'"),
        {"i": gate_invoice.id},
    ).scalar_one()
    assert total_revoked == 2, "two actions are revoked in total"
    assert result.revoked_actions == 1, "but this settlement revoked exactly one"
    assert unrelated is not None
