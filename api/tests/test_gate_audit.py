"""Gate audit records (FR-7.9) and the structural no-send-without-a-verdict guarantee (ADR-005).

The audit entry is the compliance artefact. ADR-005: "the strongest answer to 'what if the AI goes
rogue?' is opening the audit log filtered to ``outcome = blocked`` and showing every message the
system refused to send, with reasons."
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.delivery.email import DeliveryNotConfigured
from app.delivery.sender import GateNotPassedError, assert_sendable, send
from app.enums import ActionType, Channel
from app.guardrails.gate import gate
from app.models.invoice import Invoice
from app.schemas.gate import GATE_CHECK_ORDER, CheckName, CheckResult, GateVerdict, ProposedAction
from tests.gate_support import MIDDAY_IST, NIGHT_IST, clean_message, make_action

pytestmark = pytest.mark.usefixtures("db_available")


def _gate_entries(db: Session, merchant_id: uuid.UUID) -> list[Any]:
    return db.execute(
        text(
            "SELECT action_type, outcome, gate_verdicts, rationale, subject_id "
            "FROM audit_log WHERE merchant_id = :m AND action_type LIKE 'gate.%' "
            "ORDER BY id"
        ),
        {"m": merchant_id},
    ).all()


# --- FR-7.9: every verdict, pass and fail, is logged ------------------------------------------


def test_a_passing_gate_writes_one_entry_with_seven_verdicts(
    db_session: Session, gate_invoice: Invoice, gate_consent: Any, gate_merchant: Any
) -> None:
    verdict = gate(db_session, make_action(gate_invoice), now=MIDDAY_IST)
    assert verdict.passed, verdict.blocked_by

    entries = _gate_entries(db_session, gate_merchant.id)
    assert len(entries) == 1, "exactly one audit entry per gate call"
    action_type, outcome, verdicts, _rationale, subject_id = entries[0]
    assert action_type == "gate.send_message"
    # 'approved', not 'executed': passing the gate is authorisation, not delivery.
    assert outcome == "approved"
    assert len(verdicts) == 7
    assert [v["check"] for v in verdicts] == [c.value for c in GATE_CHECK_ORDER]
    assert subject_id == gate_invoice.id


def test_a_blocked_gate_is_logged_just_as_carefully(
    db_session: Session, gate_invoice: Invoice, gate_consent: Any, gate_merchant: Any
) -> None:
    """Refusals are logged as carefully as sends -- that is the demo moment."""
    gate(db_session, make_action(gate_invoice), now=NIGHT_IST)

    entries = _gate_entries(db_session, gate_merchant.id)
    assert len(entries) == 1
    _at, outcome, verdicts, rationale, _sid = entries[0]
    assert outcome == "blocked"
    assert len(verdicts) == 7
    assert "time_window" in rationale
    failed = [v for v in verdicts if not v["passed"]]
    assert all(v.get("reason") for v in failed), "every failure carries a reason"


def test_each_gate_call_writes_one_entry(
    db_session: Session, gate_invoice: Invoice, gate_consent: Any, gate_merchant: Any
) -> None:
    for _ in range(3):
        gate(db_session, make_action(gate_invoice), now=MIDDAY_IST)
    assert len(_gate_entries(db_session, gate_merchant.id)) == 3


def test_the_gate_entry_joins_the_hash_chain(
    db_session: Session, gate_invoice: Invoice, gate_consent: Any, gate_merchant: Any
) -> None:
    """A gate verdict is only evidence if it is tamper-evident like everything else."""
    from app.audit.log import verify_chain

    gate(db_session, make_action(gate_invoice), now=MIDDAY_IST)
    gate(db_session, make_action(gate_invoice, tone_tier=2), now=MIDDAY_IST)
    assert verify_chain(db_session, gate_merchant.id)


# --- the done-when scenario -------------------------------------------------------------------


def test_an_action_failing_checks_1_4_and_7_shows_all_seven_verdicts(
    db_session: Session,
    gate_invoice: Invoice,
    gate_counterparty: Any,
    gate_merchant: Any,
    gate_consent: Any,
) -> None:
    """Three simultaneous failures, seven verdicts recorded, nothing sent.

    * check 1 fails: 03:00 IST, outside the contact window
    * check 4 fails: 7 lifetime touches against a cap of 6
    * check 7 fails: 3 broken promises, the absolute stopping rule
    """
    gate_invoice.touch_count = 7
    gate_counterparty.broken_promise_count = 3
    db_session.flush()

    action = make_action(gate_invoice)
    verdict = gate(db_session, action, now=NIGHT_IST)

    assert not verdict.passed
    assert len(verdict.checks) == 7
    assert set(verdict.blocked_by) == {"time_window", "frequency_cap", "stopping_rules"}

    # The four that passed genuinely evaluated rather than being skipped.
    for name in (
        CheckName.FRESHNESS,
        CheckName.CONSENT,
        CheckName.VALUE_THRESHOLD,
        CheckName.CONTENT_POLICY,
    ):
        assert verdict.result_for(name).passed
        assert verdict.result_for(name).detail, f"{name} produced no evidence"

    # ...and all seven, with the three reasons, are in the audit log.
    entries = _gate_entries(db_session, gate_merchant.id)
    assert len(entries) == 1
    _at, outcome, verdicts, rationale, _sid = entries[0]
    assert outcome == "blocked"
    assert len(verdicts) == 7
    failures = {v["check"]: v["reason"] for v in verdicts if not v["passed"]}
    assert set(failures) == {"time_window", "frequency_cap", "stopping_rules"}
    assert "outside 08:00-19:00" in failures["time_window"]
    assert "exceeds the cap" in failures["frequency_cap"]
    assert "broken_promises_exceeded" in failures["stopping_rules"]
    for check in ("time_window", "frequency_cap", "stopping_rules"):
        assert check in rationale

    # And the action cannot be executed.
    with pytest.raises(GateNotPassedError):
        send(action, verdict)


# --- the structural guarantee: no send without a passed verdict -------------------------------


def test_send_requires_a_verdict_argument() -> None:
    """ADR-005: enforce it in the signature, not by convention."""
    import inspect

    params = inspect.signature(send).parameters
    assert "verdict" in params
    assert params["verdict"].default is inspect.Parameter.empty, "verdict must be required"


def test_send_refuses_a_blocked_verdict(
    db_session: Session, gate_invoice: Invoice, gate_consent: Any
) -> None:
    action = make_action(gate_invoice)
    verdict = gate(db_session, action, now=NIGHT_IST)
    with pytest.raises(GateNotPassedError, match="time_window"):
        send(action, verdict)


def test_send_refuses_a_verdict_for_a_different_invoice(
    db_session: Session,
    gate_invoice: Invoice,
    gate_consent: Any,
    gate_counterparty: Any,
    gate_merchant: Any,
) -> None:
    """A passing verdict must not be reusable across invoices -- an easy accident in a loop."""
    other = Invoice(
        id=uuid.uuid4(),
        merchant_id=gate_merchant.id,
        counterparty_id=gate_counterparty.id,
        invoice_number="INV-GATE-002",
        amount_paise=50_000,
        outstanding_paise=50_000,
        issue_date=date(2026, 6, 1),
        due_date=date(2026, 7, 1),
        terms_days=30,
    )
    db_session.add(other)
    db_session.flush()

    verdict = gate(db_session, make_action(gate_invoice), now=MIDDAY_IST)
    assert verdict.passed
    with pytest.raises(GateNotPassedError, match="invoice"):
        send(make_action(other), verdict)


def test_send_refuses_a_verdict_for_a_different_action_type(
    db_session: Session, gate_invoice: Invoice, gate_consent: Any
) -> None:
    verdict = gate(db_session, make_action(gate_invoice), now=MIDDAY_IST)
    other = make_action(
        gate_invoice,
        type=ActionType.SWITCH_CHANNEL,
        channel=Channel.SMS,
        message=clean_message(gate_invoice, channel=Channel.SMS),
    )
    with pytest.raises(GateNotPassedError, match="action"):
        send(other, verdict)


def test_send_refuses_a_stale_verdict(
    db_session: Session, gate_invoice: Invoice, gate_consent: Any
) -> None:
    """A verdict is a statement about a moment; check 2 exists because minutes matter."""
    action = make_action(gate_invoice)
    verdict = gate(db_session, action, now=MIDDAY_IST)
    assert verdict.passed
    with pytest.raises(GateNotPassedError, match="old"):
        send(action, verdict, now=MIDDAY_IST + timedelta(hours=2))


def test_a_passing_current_matching_verdict_reaches_the_transport(
    db_session: Session, gate_invoice: Invoice, gate_consent: Any
) -> None:
    """The precondition passes and the call reaches the transport.

    It then stops at ``DeliveryNotConfigured`` rather than ``NotImplementedError``, because
    Phase 6.5 wired Resend behind this and the suite disables it (see ``_no_real_email``). What
    matters is *where* it stops: past ``assert_sendable``, at the provider — not at the gate.
    """
    action = make_action(gate_invoice)
    verdict = gate(db_session, action, now=MIDDAY_IST)
    assert verdict.passed

    assert_sendable(action, verdict, now=MIDDAY_IST + timedelta(seconds=30))
    with pytest.raises(DeliveryNotConfigured):
        send(action, verdict, now=MIDDAY_IST + timedelta(seconds=30))


def test_a_hand_built_passing_verdict_still_has_to_match(
    db_session: Session, gate_invoice: Invoice
) -> None:
    """Constructing a verdict by hand does not get you a send for a different invoice."""
    forged = GateVerdict(
        invoice_id=uuid.uuid4(),  # not this invoice
        checks=[CheckResult(check=c, passed=True) for c in GATE_CHECK_ORDER],
        evaluated_at=MIDDAY_IST,
        action_type=ActionType.SEND_MESSAGE,
    )
    assert forged.passed
    with pytest.raises(GateNotPassedError, match="invoice"):
        send(make_action(gate_invoice), forged, now=MIDDAY_IST)


def test_no_transport_call_lives_outside_sender(  # noqa: D401
) -> None:
    """No module may call a delivery provider directly, bypassing the precondition.

    Matches on provider *call* shapes rather than bare names: ``config.py`` legitimately holds
    ``MSG91_API_KEY``, and a guard that fires on settings keys would be turned off within a week.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    markers = ("resend.emails", "api.msg91.com", "graph.facebook.com", "smtplib.smtp")
    offenders = [
        f"{path.relative_to(root)}: {marker}"
        for path in root.rglob("*.py")
        if path.name != "sender.py"
        for marker in markers
        if marker in path.read_text(encoding="utf-8").lower()
    ]
    assert not offenders, f"delivery calls outside sender.py: {offenders}"


def test_the_gate_never_mutates_the_draft(
    db_session: Session, gate_invoice: Invoice, gate_consent: Any
) -> None:
    """The gate inspects a draft; it never writes one. Generation is Phase 5."""
    message = clean_message(gate_invoice)
    before = message.model_dump()
    action = ProposedAction(
        invoice_id=gate_invoice.id,
        type=ActionType.SEND_MESSAGE,
        tone_tier=1,
        rationale="test",
        channel=Channel.EMAIL,
        message=message,
    )
    gate(db_session, action, now=MIDDAY_IST)
    assert action.message is not None
    assert action.message.model_dump() == before, "the gate mutated the draft"


# --- C2: a passing gate authorises, it does not claim execution ---


def test_a_passing_gate_writes_approved_not_executed(
    db_session: Session, gate_invoice: Invoice, gate_consent: Any, gate_merchant: Any
) -> None:
    """Passing the gate is authorisation, not delivery.

    A crash between gate and send must not leave the audit log claiming a message went out. The
    log may under-claim; it may never over-claim.
    """
    from app.guardrails.gate import GATE_PASSED_OUTCOME

    verdict = gate(db_session, make_action(gate_invoice), now=MIDDAY_IST)
    assert verdict.passed

    outcome = _gate_entries(db_session, gate_merchant.id)[0][1]
    assert outcome == "approved"
    assert outcome == GATE_PASSED_OUTCOME
    assert outcome != "executed", "the gate must never claim a send happened"


def test_gate_outcomes_are_in_the_documented_vocabulary(
    db_session: Session, gate_invoice: Invoice, gate_consent: Any, gate_merchant: Any
) -> None:
    """architecture/data-model.md: executed | blocked | stopped | approved | rejected."""
    documented = {"executed", "blocked", "stopped", "approved", "rejected"}
    gate(db_session, make_action(gate_invoice), now=MIDDAY_IST)
    gate(db_session, make_action(gate_invoice), now=NIGHT_IST)

    outcomes = {row[1] for row in _gate_entries(db_session, gate_merchant.id)}
    assert outcomes == {"approved", "blocked"}
    assert outcomes <= documented


def test_nothing_writes_an_executed_entry_before_the_transport_exists(
    db_session: Session, gate_invoice: Invoice, gate_consent: Any, gate_merchant: Any
) -> None:
    """Until delivery is implemented, no code path may record a send."""
    gate(db_session, make_action(gate_invoice), now=MIDDAY_IST)
    executed = db_session.execute(
        text("SELECT count(*) FROM audit_log WHERE merchant_id = :m AND outcome = 'executed'"),
        {"m": gate_merchant.id},
    ).scalar_one()
    assert executed == 0
