"""The planning half of Phase 6: the closed registry, diagnosis, and proposal.

These are the decisions made *before* the gate sees anything. Nothing here decides whether an
action may fire -- that is the gate's job and is tested in ``test_gate.py``. What is tested here is
that the agent proposes exactly one well-formed, legal action, and that the deterministic path
alone produces one when no model is available.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import Session

from app.agent import propose as propose_mod
from app.agent import registry
from app.agent.diagnose import Signals, diagnose, infer
from app.enums import ActionType, Channel, PaymentStatus, RecoveryState, UnpaidCause
from app.models.counterparty import Counterparty
from app.models.invoice import Invoice

pytestmark = pytest.mark.usefixtures("db_available")


def _signals(**overrides: object) -> Signals:
    base: dict[str, object] = {
        "link_opened_unpaid_count": 0,
        "email_bounced": False,
        "zero_engagement": False,
        "partial_payment_received": False,
        "historically_reliable": False,
        "first_slip": False,
        "days_past_due": 30,
        "touch_count": 1,
    }
    base.update(overrides)
    return Signals(**base)  # type: ignore[arg-type]


# --- the closed registry ------------------------------------------------------------------------


def test_an_unregistered_tool_is_not_registered() -> None:
    """The first validate check: anything off the list is rejected before it reaches the gate."""
    assert not registry.is_registered("wire_transfer")
    assert not registry.is_registered("DROP TABLE invoices")


def test_every_action_type_the_enum_knows_is_in_the_registry() -> None:
    """A tool that exists as an enum value but not in the registry is unreachable and confusing."""
    assert set(registry.REGISTRY) == set(ActionType)


def test_stop_is_allowed_from_every_state() -> None:
    """The registry's asymmetry: the system may always choose to do less."""
    for state in RecoveryState:
        assert registry.transition_allowed(ActionType.STOP, state)


def test_outreach_is_not_allowed_from_a_terminal_state() -> None:
    for state in (RecoveryState.SETTLED, RecoveryState.STOPPED):
        assert not registry.transition_allowed(ActionType.SEND_MESSAGE, state)


def test_outreach_is_not_allowed_while_a_human_holds_the_account() -> None:
    assert not registry.transition_allowed(ActionType.SEND_MESSAGE, RecoveryState.HUMAN_REVIEW)


def test_unimplemented_tools_stay_registered() -> None:
    """Removing them would make a proposal naming one look like a hallucination.

    A real capability that is not wired yet and an invented tool need different responses -- one
    is a roadmap entry, the other is a model failure.
    """
    assert ActionType.OFFER_INSTALLMENT in registry.REGISTRY
    assert ActionType.OFFER_INSTALLMENT in registry.UNIMPLEMENTED


# --- diagnosis ----------------------------------------------------------------------------------


def test_a_bounced_email_outranks_an_opened_link() -> None:
    """Precedence matters: a message that never arrived says nothing about willingness to pay."""
    cause, confidence, _ = infer(_signals(email_bounced=True, link_opened_unpaid_count=5))
    assert cause is UnpaidCause.WRONG_CONTACT
    assert confidence == "high"


def test_a_partial_payment_reads_as_cash_crunch() -> None:
    cause, _, _ = infer(_signals(partial_payment_received=True))
    assert cause is UnpaidCause.CASH_CRUNCH


def test_repeatedly_opening_a_link_without_paying_reads_as_cash_crunch() -> None:
    cause, _, _ = infer(_signals(link_opened_unpaid_count=2))
    assert cause is UnpaidCause.CASH_CRUNCH


def test_a_reliable_payers_first_slip_is_an_oversight() -> None:
    cause, _, _ = infer(
        _signals(historically_reliable=True, first_slip=True, days_past_due=4)
    )
    assert cause is UnpaidCause.OVERSIGHT


def test_no_distinguishing_signal_is_unknown_not_a_guess() -> None:
    cause, confidence, _ = infer(_signals())
    assert cause is UnpaidCause.UNKNOWN
    assert confidence == "low"


def test_a_recorded_dispute_is_not_re_derived_from_signals(
    db_session: Session, gate_invoice: Invoice, gate_counterparty: Counterparty
) -> None:
    """A stated fact outranks a behavioural guess.

    ``dispute`` comes from a human or the counterparty saying so outright. Overwriting it with an
    inference would resume chasing someone who has formally objected.
    """
    gate_invoice.inferred_cause = UnpaidCause.DISPUTE.value
    gate_invoice.payment_status = PaymentStatus.PARTIALLY_PAID.value
    db_session.flush()

    result = diagnose(db_session, gate_invoice, gate_counterparty)
    assert result.cause is UnpaidCause.DISPUTE


# --- proposal -----------------------------------------------------------------------------------


def test_the_escalation_ladder_is_attempt_to_tier() -> None:
    """FR-16.6, the whole escalation design."""
    assert propose_mod.tier_for_attempt(1) == 1
    assert propose_mod.tier_for_attempt(2) == 2
    assert propose_mod.tier_for_attempt(3) == 3


def test_the_ladder_never_climbs_past_the_agents_ceiling() -> None:
    """Tier 4 exists but is reserved for a human."""
    assert propose_mod.tier_for_attempt(9) == propose_mod.MAX_AGENT_TIER


def test_attempt_number_counts_from_touches_already_made(gate_invoice: Invoice) -> None:
    gate_invoice.touch_count = 2
    assert propose_mod.attempt_number(gate_invoice) == 3


def test_a_disputed_invoice_is_frozen_not_chased(gate_invoice: Invoice) -> None:
    action = propose_mod.policy_action(gate_invoice, UnpaidCause.DISPUTE, Channel.EMAIL)
    assert action.type is ActionType.MARK_DISPUTED


def test_a_refusal_stops_permanently(gate_invoice: Invoice) -> None:
    action = propose_mod.policy_action(gate_invoice, UnpaidCause.REFUSAL, Channel.EMAIL)
    assert action.type is ActionType.STOP


def test_an_exhausted_ladder_stops_rather_than_escalating_further(gate_invoice: Invoice) -> None:
    """Beyond tier 3 the next move is a human's, not a firmer message."""
    gate_invoice.touch_count = 6
    action = propose_mod.policy_action(gate_invoice, UnpaidCause.UNKNOWN, Channel.EMAIL)
    assert action.type is ActionType.STOP


def test_the_policy_never_proposes_an_illegal_transition(gate_invoice: Invoice) -> None:
    """The deterministic path runs when every provider is down; it must obey its own registry.

    Otherwise the fallback is the only path that can propose something the validator would reject.
    """
    gate_invoice.recovery_state = RecoveryState.HUMAN_REVIEW.value
    gate_invoice.touch_count = 1
    action = propose_mod.policy_action(gate_invoice, UnpaidCause.UNKNOWN, Channel.EMAIL)

    assert action.type is not ActionType.SEND_MESSAGE
    ok, reason = propose_mod.validate(action, gate_invoice)
    assert ok, reason


def test_a_normal_overdue_invoice_gets_a_message_at_the_attempt_tier(
    gate_invoice: Invoice,
) -> None:
    gate_invoice.recovery_state = RecoveryState.CHASING.value
    gate_invoice.touch_count = 1
    action = propose_mod.policy_action(gate_invoice, UnpaidCause.OVERSIGHT, Channel.EMAIL)

    assert action.type is ActionType.SEND_MESSAGE
    assert action.tone_tier == 2
    assert action.channel is Channel.EMAIL


# --- validate -----------------------------------------------------------------------------------


def test_validate_rejects_a_tool_outside_the_registry(gate_invoice: Invoice) -> None:
    from app.schemas.gate import ProposedAction

    candidate = ProposedAction(
        invoice_id=gate_invoice.id,
        type=ActionType.LOG_PROMISE,  # registered but not executable in this phase
        tone_tier=1,
        rationale="x",
    )
    ok, reason = propose_mod.validate(candidate, gate_invoice)
    assert not ok
    assert "not executable" in (reason or "")


def test_validate_rejects_an_action_for_a_different_invoice(gate_invoice: Invoice) -> None:
    from app.schemas.gate import ProposedAction

    candidate = ProposedAction(
        invoice_id=uuid.uuid4(),
        type=ActionType.STOP,
        tone_tier=1,
        rationale="x",
    )
    ok, reason = propose_mod.validate(candidate, gate_invoice)
    assert not ok
    assert "different invoice" in (reason or "")


def test_validate_rejects_an_outbound_action_with_no_channel(gate_invoice: Invoice) -> None:
    from app.schemas.gate import ProposedAction

    gate_invoice.recovery_state = RecoveryState.CHASING.value
    candidate = ProposedAction(
        invoice_id=gate_invoice.id,
        type=ActionType.SEND_MESSAGE,
        tone_tier=1,
        rationale="x",
        channel=None,
    )
    ok, reason = propose_mod.validate(candidate, gate_invoice)
    assert not ok
    assert "channel" in (reason or "")


def test_propose_falls_back_to_policy_when_no_model_is_available(
    gate_invoice: Invoice, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLAUDE.md invariant 9, at the proposal layer."""
    monkeypatch.setattr(propose_mod, "available", lambda job: False)
    gate_invoice.recovery_state = RecoveryState.CHASING.value

    proposal = propose_mod.propose(gate_invoice, UnpaidCause.OVERSIGHT)
    assert proposal.source == "policy"
    assert proposal.action.type is ActionType.SEND_MESSAGE


def test_a_model_proposing_an_unregistered_tool_is_discarded(
    gate_invoice: Invoice, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rejection is recorded, not silently swallowed -- showing one on stage is worth having."""
    monkeypatch.setattr(propose_mod, "available", lambda job: True)
    monkeypatch.setattr(
        propose_mod,
        "llm_action",
        lambda invoice, cause, channel: (
            propose_mod.ProposedAction(
                invoice_id=invoice.id,
                type=ActionType.LOG_PROMISE,
                tone_tier=1,
                rationale="model wants this",
            ),
            "test/model",
            None,
        ),
    )
    gate_invoice.recovery_state = RecoveryState.CHASING.value

    proposal = propose_mod.propose(gate_invoice, UnpaidCause.UNKNOWN)
    assert proposal.source == "policy"
    assert proposal.rejection_reason is not None
