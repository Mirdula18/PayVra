"""The guardrail gate (ADR-005, FR-7). This module carries the compliance claim.

Every test runs against a live database inside a rolled-back transaction, and against
hand-constructed :class:`ProposedAction` objects -- the gate must be verifiable without an agent,
a scheduler, or an LLM anywhere in the picture.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.clock import IST
from app.enums import ActionType, Channel, PaymentStatus, RecoveryState, StopReason
from app.guardrails import gate as gate_mod
from app.guardrails.gate import gate
from app.models.consent import Consent
from app.models.counterparty import Counterparty
from app.models.invoice import Invoice
from app.models.merchant import Merchant
from app.schemas.gate import GATE_CHECK_ORDER, CheckName, DraftMessage, ProposedAction
from tests.gate_support import LINK, MIDDAY_IST, OPT_OUT, clean_body, make_action

pytestmark = pytest.mark.usefixtures("db_available")


# --- the structural invariant -----------------------------------------------------------------


def test_a_verdict_always_carries_all_seven_checks_in_order(
    db_session: Session, gate_invoice: Invoice, gate_consent: Consent
) -> None:
    verdict = gate(db_session, make_action(gate_invoice), now=MIDDAY_IST)
    assert len(verdict.checks) == 7
    assert tuple(c.check for c in verdict.checks) == GATE_CHECK_ORDER


def test_all_seven_run_even_when_check_1_fails(
    db_session: Session, gate_invoice: Invoice, gate_consent: Consent
) -> None:
    """ADR-005: a partial verdict is a weaker audit record and a weaker demo."""
    night = datetime(2026, 8, 24, 3, 0, tzinfo=IST)
    verdict = gate(db_session, make_action(gate_invoice), now=night)

    assert not verdict.passed
    assert not verdict.result_for(CheckName.TIME_WINDOW).passed
    assert len(verdict.checks) == 7
    # The later checks are real evaluations, not filler: they carry detail from actual reads.
    assert verdict.result_for(CheckName.FRESHNESS).detail["payment_status"] == "unpaid"
    assert verdict.result_for(CheckName.CONSENT).passed
    assert "touches_this_week" in verdict.result_for(CheckName.FREQUENCY_CAP).detail


def test_a_partial_verdict_cannot_be_constructed() -> None:
    """The invariant is enforced by the model, so no future early return can produce one."""
    from app.schemas.gate import CheckResult, GateVerdict

    with pytest.raises(ValueError, match="all 7 checks"):
        GateVerdict(
            invoice_id=uuid.uuid4(),
            checks=[CheckResult(check=CheckName.TIME_WINDOW, passed=True)],
            evaluated_at=MIDDAY_IST,
            action_type=ActionType.SEND_MESSAGE,
        )


def test_a_reordered_verdict_cannot_be_constructed() -> None:
    from app.schemas.gate import CheckResult, GateVerdict

    reordered = list(reversed(GATE_CHECK_ORDER))
    with pytest.raises(ValueError, match="ADR-005 order"):
        GateVerdict(
            invoice_id=uuid.uuid4(),
            checks=[CheckResult(check=c, passed=True) for c in reordered],
            evaluated_at=MIDDAY_IST,
            action_type=ActionType.SEND_MESSAGE,
        )


def test_the_gate_contains_no_llm_call() -> None:
    """ADR-005: "a compliance control a language model can be talked out of is not a control"."""
    import ast
    import pathlib

    from app.guardrails import policy_content, stopping

    for module in (gate_mod, policy_content, stopping):
        tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        banned = {"litellm", "openai", "groq", "google.generativeai", "anthropic"}
        assert not (imported & banned), f"{module.__name__} imports an LLM client"
        assert not any("llm" in name.lower() for name in imported), module.__name__


# --- check 1: time window ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [(7, 59, False), (8, 0, True), (18, 59, True), (19, 0, False)],
)
def test_time_window_boundaries_ist(
    db_session: Session,
    gate_invoice: Invoice,
    gate_consent: Consent,
    hour: int,
    minute: int,
    expected: bool,
) -> None:
    moment = datetime(2026, 8, 24, hour, minute, tzinfo=IST)
    verdict = gate(db_session, make_action(gate_invoice), now=moment)
    assert verdict.result_for(CheckName.TIME_WINDOW).passed is expected


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [(7, 59, False), (8, 0, True), (18, 59, True), (19, 0, False)],
)
def test_the_same_wall_clock_in_utc_does_not_flip_the_result(
    db_session: Session,
    gate_invoice: Invoice,
    gate_consent: Consent,
    hour: int,
    minute: int,
    expected: bool,
) -> None:
    """The window is IST. Passing the same instant expressed in UTC must not change the verdict.

    If the gate ever compared a UTC hour, 08:00 IST would read as 02:30 and every send would be
    blocked -- or worse, 02:30 IST would read as 21:00 and messages would go out overnight.
    """
    ist_moment = datetime(2026, 8, 24, hour, minute, tzinfo=IST)
    utc_moment = ist_moment.astimezone(UTC)
    assert utc_moment.hour != ist_moment.hour, "the test instants must actually differ in UTC"

    from_ist = gate(db_session, make_action(gate_invoice), now=ist_moment)
    from_utc = gate(db_session, make_action(gate_invoice), now=utc_moment)
    assert from_ist.result_for(CheckName.TIME_WINDOW).passed is expected
    assert from_utc.result_for(CheckName.TIME_WINDOW).passed is expected


def test_non_outbound_actions_are_exempt_from_the_window(
    db_session: Session, gate_invoice: Invoice
) -> None:
    """Logging a promise at 03:00 contacts nobody."""
    night = datetime(2026, 8, 24, 3, 0, tzinfo=IST)
    action = make_action(gate_invoice, type=ActionType.LOG_PROMISE, message=None, channel=None)
    verdict = gate(db_session, action, now=night)
    assert verdict.result_for(CheckName.TIME_WINDOW).passed


# --- check 2: freshness -----------------------------------------------------------------------


def test_freshness_blocks_an_invoice_paid_between_proposal_and_dispatch(
    db_session: Session, gate_invoice: Invoice, gate_consent: Consent
) -> None:
    """The worst failure mode in the product, with a real time gap rather than a mock.

    The action is built at "01:30" against an unpaid gate_invoice. The payment then lands. The gate
    runs at "09:00" and must see the payment -- from the database, not from the action.
    """
    planning_time = datetime(2026, 8, 24, 1, 30, tzinfo=IST)
    action = make_action(gate_invoice)
    assert gate_invoice.payment_status == PaymentStatus.UNPAID.value

    # Nothing about the action changes; the world does. Written via SQL so the ORM object the
    # action was built from is genuinely stale, exactly as it would be after hours in a queue.
    db_session.execute(
        text(
            "UPDATE invoices SET payment_status = 'paid', outstanding_paise = 0, "
            "settled_at = now() WHERE id = :i"
        ),
        {"i": gate_invoice.id},
    )
    db_session.flush()

    dispatch_time = datetime(2026, 8, 24, 9, 0, tzinfo=IST)
    assert dispatch_time - planning_time > timedelta(hours=7)

    verdict = gate(db_session, action, now=dispatch_time)
    freshness = verdict.result_for(CheckName.FRESHNESS)
    assert not freshness.passed
    assert "paid" in (freshness.reason or "")
    assert not verdict.passed


def test_freshness_reads_the_database_not_the_passed_object(
    db_session: Session, gate_invoice: Invoice, gate_consent: Consent
) -> None:
    """Even with a stale ORM instance held in the session's identity map."""
    action = make_action(gate_invoice)
    db_session.execute(
        text("UPDATE invoices SET payment_status = 'paid', outstanding_paise = 0 WHERE id = :i"),
        {"i": gate_invoice.id},
    )
    # Deliberately do NOT refresh; the in-memory copy still says unpaid.
    assert gate_invoice.payment_status == PaymentStatus.UNPAID.value

    verdict = gate(db_session, action, now=MIDDAY_IST)
    assert not verdict.result_for(CheckName.FRESHNESS).passed


def test_freshness_blocks_when_nothing_is_outstanding(
    db_session: Session, gate_invoice: Invoice, gate_consent: Consent
) -> None:
    db_session.execute(
        text("UPDATE invoices SET outstanding_paise = 0 WHERE id = :i"), {"i": gate_invoice.id}
    )
    verdict = gate(db_session, make_action(gate_invoice), now=MIDDAY_IST)
    assert not verdict.result_for(CheckName.FRESHNESS).passed


# --- check 3: consent -------------------------------------------------------------------------


def test_consent_blocks_a_revoked_channel(
    db_session: Session, gate_invoice: Invoice, gate_consent: Consent
) -> None:
    gate_consent.revoked_at = datetime(2026, 8, 1, tzinfo=UTC)
    db_session.flush()
    verdict = gate(db_session, make_action(gate_invoice), now=MIDDAY_IST)
    result = verdict.result_for(CheckName.CONSENT)
    assert not result.passed
    assert "revoked" in (result.reason or "")


def test_consent_blocks_a_quarantined_counterparty(
    db_session: Session,
    gate_invoice: Invoice,
    gate_counterparty: Counterparty,
    gate_consent: Consent,
) -> None:
    """FR-2.3: no confirmed consent basis means never contacted."""
    gate_counterparty.is_quarantined = True
    db_session.flush()
    verdict = gate(db_session, make_action(gate_invoice), now=MIDDAY_IST)
    result = verdict.result_for(CheckName.CONSENT)
    assert not result.passed
    assert "quarantined" in (result.reason or "")


def test_consent_blocks_the_wrong_channel(
    db_session: Session, gate_invoice: Invoice, gate_consent: Consent
) -> None:
    """Email is permitted; WhatsApp has no gate_consent record at all."""
    message = DraftMessage(
        channel=Channel.WHATSAPP,
        body=clean_body(gate_invoice),
        quoted_amount_paise=gate_invoice.outstanding_paise,
        quoted_invoice_number=gate_invoice.invoice_number,
        payment_link_url=LINK,
        opt_out_url=OPT_OUT,
        sender_name="GateTest Supplies",
    )
    action = make_action(gate_invoice, channel=Channel.WHATSAPP, message=message)
    verdict = gate(db_session, action, now=MIDDAY_IST)
    result = verdict.result_for(CheckName.CONSENT)
    assert not result.passed
    assert "whatsapp" in (result.reason or "").lower()


def test_consent_blocks_a_channel_marked_not_permitted(
    db_session: Session, gate_invoice: Invoice, gate_consent: Consent
) -> None:
    gate_consent.is_permitted = False
    db_session.flush()
    verdict = gate(db_session, make_action(gate_invoice), now=MIDDAY_IST)
    assert not verdict.result_for(CheckName.CONSENT).passed


# --- check 4: frequency cap -------------------------------------------------------------------


def _record_touches(db: Session, gate_invoice: Invoice, count: int, *, days_ago: int = 1) -> None:
    """Executed actions against this gate_counterparty, inside the rolling week."""
    executed = datetime.now(UTC) - timedelta(days=days_ago)
    for _ in range(count):
        db.execute(
            text(
                "INSERT INTO actions (id, merchant_id, invoice_id, type, status, proposed_by, "
                "rationale, scheduled_for, executed_at) VALUES "
                "(:id, :m, :i, 'send_message', 'executed', 'agent', 'test', :t, :t)"
            ),
            {
                "id": uuid.uuid4(),
                "m": gate_invoice.merchant_id,
                "i": gate_invoice.id,
                "t": executed,
            },
        )
    db.flush()


@pytest.mark.parametrize(("touches", "expected"), [(0, True), (2, True), (3, False)])
def test_weekly_frequency_cap_boundary(
    db_session: Session, gate_invoice: Invoice, gate_consent: Consent, touches: int, expected: bool
) -> None:
    """ADR-005 fails on ">2 this week": exactly 2 passes, 3 blocks."""
    _record_touches(db_session, gate_invoice, touches)
    verdict = gate(db_session, make_action(gate_invoice), now=MIDDAY_IST)
    assert verdict.result_for(CheckName.FREQUENCY_CAP).passed is expected


@pytest.mark.parametrize(("lifetime", "expected"), [(6, True), (7, False)])
def test_lifetime_frequency_cap_boundary(
    db_session: Session, gate_invoice: Invoice, gate_consent: Consent, lifetime: int, expected: bool
) -> None:
    """Exactly 6 lifetime passes, 7 blocks."""
    gate_invoice.touch_count = lifetime
    db_session.flush()
    verdict = gate(db_session, make_action(gate_invoice), now=MIDDAY_IST)
    assert verdict.result_for(CheckName.FREQUENCY_CAP).passed is expected


def test_touches_older_than_a_week_do_not_count(
    db_session: Session, gate_invoice: Invoice, gate_consent: Consent
) -> None:
    _record_touches(db_session, gate_invoice, 5, days_ago=30)
    verdict = gate(db_session, make_action(gate_invoice), now=MIDDAY_IST)
    assert verdict.result_for(CheckName.FREQUENCY_CAP).passed


# --- check 5: value threshold -----------------------------------------------------------------


def test_value_threshold_blocks_above_the_merchant_limit(
    db_session: Session, gate_invoice: Invoice, gate_merchant: Merchant, gate_consent: Consent
) -> None:
    gate_merchant.approval_value_threshold_paise = 1_00_000
    db_session.flush()
    verdict = gate(db_session, make_action(gate_invoice), now=MIDDAY_IST)
    result = verdict.result_for(CheckName.VALUE_THRESHOLD)
    assert not result.passed
    assert "approval" in (result.reason or "")


def test_tone_tier_3_requires_approval_regardless_of_value(
    db_session: Session, gate_invoice: Invoice, gate_consent: Consent
) -> None:
    """FR-7.6: a tone ceiling independent of the amount."""
    verdict = gate(db_session, make_action(gate_invoice, tone_tier=3), now=MIDDAY_IST)
    assert not verdict.result_for(CheckName.VALUE_THRESHOLD).passed


def test_stopping_a_high_value_invoice_never_needs_approval(
    db_session: Session, gate_invoice: Invoice, gate_merchant: Merchant, gate_consent: Consent
) -> None:
    """The registry's asymmetry: the system may always choose to do less.

    Requiring permission to *stop* contacting a high-value counterparty inverts the rule -- the
    safest action becomes the blocked one, and the account sits in limbo. Approval governs
    contact, and stopping is the absence of contact.
    """
    gate_merchant.approval_value_threshold_paise = 1_00_000
    db_session.flush()
    verdict = gate(
        db_session,
        make_action(gate_invoice, type=ActionType.STOP, channel=None),
        now=MIDDAY_IST,
    )
    assert verdict.result_for(CheckName.VALUE_THRESHOLD).passed


def test_a_high_value_snooze_is_also_exempt(
    db_session: Session, gate_invoice: Invoice, gate_merchant: Merchant, gate_consent: Consent
) -> None:
    """Same reasoning as stop: deferring contact is not contact."""
    gate_merchant.approval_value_threshold_paise = 1_00_000
    db_session.flush()
    verdict = gate(
        db_session,
        make_action(gate_invoice, type=ActionType.SNOOZE, channel=None),
        now=MIDDAY_IST,
    )
    assert verdict.result_for(CheckName.VALUE_THRESHOLD).passed


def test_an_outbound_action_is_still_gated_on_value(
    db_session: Session, gate_invoice: Invoice, gate_merchant: Merchant, gate_consent: Consent
) -> None:
    """The exemption must not leak: contacting someone still needs approval above the threshold."""
    gate_merchant.approval_value_threshold_paise = 1_00_000
    db_session.flush()
    verdict = gate(db_session, make_action(gate_invoice), now=MIDDAY_IST)
    assert not verdict.result_for(CheckName.VALUE_THRESHOLD).passed


def test_human_approval_releases_the_threshold(
    db_session: Session, gate_invoice: Invoice, gate_merchant: Merchant, gate_consent: Consent
) -> None:
    gate_merchant.approval_value_threshold_paise = 1_00_000
    db_session.flush()
    action = make_action(gate_invoice, tone_tier=3, approved_by="finance@gate_merchant.test")
    verdict = gate(db_session, action, now=MIDDAY_IST)
    assert verdict.result_for(CheckName.VALUE_THRESHOLD).passed


# --- check 6: content policy ------------------------------------------------------------------


def _with_body(invoice: Invoice, body: str, **kw: object) -> ProposedAction:
    message = DraftMessage(
        channel=Channel.EMAIL,
        body=body,
        quoted_amount_paise=kw.pop("quoted_amount_paise", invoice.outstanding_paise),  # type: ignore[arg-type]
        quoted_invoice_number=invoice.invoice_number,
        payment_link_url=kw.pop("payment_link_url", LINK),  # type: ignore[arg-type]
        opt_out_url=kw.pop("opt_out_url", OPT_OUT),  # type: ignore[arg-type]
        sender_name=kw.pop("sender_name", "GateTest Supplies"),  # type: ignore[arg-type]
    )
    return make_action(invoice, message=message, **kw)


def test_content_blocks_a_missing_payment_link(
    db_session: Session, gate_invoice: Invoice, gate_consent: Consent
) -> None:
    body = clean_body(gate_invoice).replace(f"You can pay here: {LINK}\n", "")
    action = _with_body(gate_invoice, body, payment_link_url=None)
    result = gate(db_session, action, now=MIDDAY_IST).result_for(CheckName.CONTENT_POLICY)
    assert not result.passed
    assert "payment_link" in result.detail["missing_elements"]


def test_content_blocks_a_missing_opt_out(
    db_session: Session, gate_invoice: Invoice, gate_consent: Consent
) -> None:
    """FR-2.4: every outbound message carries an opt-out."""
    body = clean_body(gate_invoice).replace(f"To stop receiving these, use {OPT_OUT}\n", "")
    action = _with_body(gate_invoice, body, opt_out_url=None)
    result = gate(db_session, action, now=MIDDAY_IST).result_for(CheckName.CONTENT_POLICY)
    assert not result.passed
    assert "opt_out" in result.detail["missing_elements"]


def test_content_blocks_a_wrong_amount(
    db_session: Session, gate_invoice: Invoice, gate_consent: Consent
) -> None:
    """A draft written before a partial payment landed must not go out with the old figure."""
    db_session.execute(
        text("UPDATE invoices SET outstanding_paise = 500000 WHERE id = :i"), {"i": gate_invoice.id}
    )
    db_session.flush()
    action = make_action(gate_invoice)  # body and quote still say the original amount
    result = gate(db_session, action, now=MIDDAY_IST).result_for(CheckName.CONTENT_POLICY)
    assert not result.passed
    assert "amount" in result.detail["missing_elements"]


def test_content_blocks_a_missing_invoice_number(
    db_session: Session, gate_invoice: Invoice, gate_consent: Consent
) -> None:
    body = clean_body(gate_invoice).replace(gate_invoice.invoice_number, "your account")
    result = gate(db_session, _with_body(gate_invoice, body), now=MIDDAY_IST).result_for(
        CheckName.CONTENT_POLICY
    )
    assert not result.passed
    assert "invoice_number" in result.detail["missing_elements"]


def test_content_blocks_a_missing_sender_identification(
    db_session: Session, gate_invoice: Invoice, gate_consent: Consent
) -> None:
    body = clean_body(gate_invoice).replace("— GateTest Supplies", "")
    action = _with_body(gate_invoice, body, sender_name=None)
    result = gate(db_session, action, now=MIDDAY_IST).result_for(CheckName.CONTENT_POLICY)
    assert not result.passed
    assert "sender_identification" in result.detail["missing_elements"]


@pytest.mark.parametrize(
    ("snippet", "category"),
    [
        ("We will take legal action against you.", "legal_threat"),
        ("This will be reported to CIBIL.", "credit_threat"),
        ("We will pursue your personal assets.", "personal_assets"),
        ("We will inform your customers about this.", "third_party_disclosure"),
        ("This is shameful conduct from your side.", "shaming"),
        ("PAY THIS INVOICE IMMEDIATELY NOW", "all_caps_demand"),
        ("This is your final notice before escalation.", "fake_urgency"),
    ],
)
def test_content_blocks_each_banned_category(
    db_session: Session, gate_invoice: Invoice, gate_consent: Consent, snippet: str, category: str
) -> None:
    action = _with_body(gate_invoice, clean_body(gate_invoice) + "\n" + snippet)
    result = gate(db_session, action, now=MIDDAY_IST).result_for(CheckName.CONTENT_POLICY)
    assert not result.passed, f"{category} was not caught"
    assert category in {v["category"] for v in result.detail["violations"]}


def test_fake_urgency_is_permitted_at_tier_4(
    db_session: Session, gate_invoice: Invoice, gate_consent: Consent
) -> None:
    """ "final notice" is only fake if the sequence has not reached the end."""
    body = clean_body(gate_invoice) + "\nThis is your final notice before we stop contacting you."
    action = _with_body(gate_invoice, body, tone_tier=4, approved_by="finance@gate_merchant.test")
    result = gate(db_session, action, now=MIDDAY_IST).result_for(CheckName.CONTENT_POLICY)
    assert result.passed, result.reason


def test_a_clean_message_passes_content_policy(
    db_session: Session, gate_invoice: Invoice, gate_consent: Consent
) -> None:
    result = gate(db_session, make_action(gate_invoice), now=MIDDAY_IST).result_for(
        CheckName.CONTENT_POLICY
    )
    assert result.passed, result.reason


def test_an_outbound_action_with_no_draft_is_blocked(
    db_session: Session, gate_invoice: Invoice, gate_consent: Consent
) -> None:
    action = make_action(gate_invoice, message=None)
    assert not gate(db_session, action, now=MIDDAY_IST).result_for(CheckName.CONTENT_POLICY).passed


# --- check 7: stopping rules ------------------------------------------------------------------


@pytest.mark.parametrize(("broken", "expected"), [(2, True), (3, False)])
def test_stopping_fires_at_exactly_three_broken_promises(
    db_session: Session,
    gate_invoice: Invoice,
    gate_counterparty: Counterparty,
    gate_consent: Consent,
    broken: int,
    expected: bool,
) -> None:
    gate_counterparty.broken_promise_count = broken
    db_session.flush()
    verdict = gate(db_session, make_action(gate_invoice), now=MIDDAY_IST)
    assert verdict.result_for(CheckName.STOPPING_RULES).passed is expected


@pytest.mark.parametrize(("touches", "expected"), [(5, True), (6, False)])
def test_stopping_fires_at_exactly_the_touch_cap(
    db_session: Session,
    gate_invoice: Invoice,
    gate_merchant: Merchant,
    gate_consent: Consent,
    touches: int,
    expected: bool,
) -> None:
    """The cap is 6, so 6 reached means stop -- a cap is a ceiling, not a budget to exceed."""
    assert gate_merchant.lifetime_touch_cap == 6
    gate_invoice.touch_count = touches
    db_session.flush()
    verdict = gate(db_session, make_action(gate_invoice), now=MIDDAY_IST)
    assert verdict.result_for(CheckName.STOPPING_RULES).passed is expected


def test_stopping_blocks_a_settled_invoice(
    db_session: Session, gate_invoice: Invoice, gate_consent: Consent
) -> None:
    db_session.execute(
        text("UPDATE invoices SET payment_status = 'paid' WHERE id = :i"), {"i": gate_invoice.id}
    )
    verdict = gate(db_session, make_action(gate_invoice), now=MIDDAY_IST)
    assert not verdict.result_for(CheckName.STOPPING_RULES).passed


def test_stopping_blocks_a_disputed_invoice(
    db_session: Session, gate_invoice: Invoice, gate_consent: Consent
) -> None:
    gate_invoice.inferred_cause = "dispute"
    db_session.flush()
    verdict = gate(db_session, make_action(gate_invoice), now=MIDDAY_IST)
    result = verdict.result_for(CheckName.STOPPING_RULES)
    assert not result.passed
    assert "disputed" in result.detail["triggers"]


def test_stopping_blocks_an_already_excluded_invoice(
    db_session: Session, gate_invoice: Invoice, gate_consent: Consent
) -> None:
    gate_invoice.recovery_state = RecoveryState.STOPPED.value
    gate_invoice.stop_reason = StopReason.MERCHANT_EXCLUDED.value
    db_session.flush()
    verdict = gate(db_session, make_action(gate_invoice), now=MIDDAY_IST)
    assert not verdict.result_for(CheckName.STOPPING_RULES).passed


def test_a_paused_merchant_blocks_but_is_marked_temporary(
    db_session: Session, gate_invoice: Invoice, gate_merchant: Merchant, gate_consent: Consent
) -> None:
    """The global kill switch. Blocks, but must not move the gate_invoice to the exception list."""
    gate_merchant.is_paused = True
    db_session.flush()
    verdict = gate(db_session, make_action(gate_invoice), now=MIDDAY_IST)
    result = verdict.result_for(CheckName.STOPPING_RULES)
    assert not result.passed
    assert result.detail["permanent"] is False
    assert result.detail["stop_reason"] is None


def test_a_single_revoked_channel_is_not_an_opt_out(
    db_session: Session,
    gate_invoice: Invoice,
    gate_counterparty: Counterparty,
    gate_consent: Consent,
) -> None:
    """FR-2.5 opt-out is all-channel. Revoking SMS while keeping email is a preference."""
    db_session.add(
        Consent(
            id=uuid.uuid4(),
            counterparty_id=gate_counterparty.id,
            channel=Channel.SMS.value,
            is_permitted=False,
            basis="existing_commercial_relationship",
            granted_at=datetime(2026, 1, 1, tzinfo=UTC),
            revoked_at=datetime(2026, 8, 1, tzinfo=UTC),
            opt_out_token="tok456",
        )
    )
    db_session.flush()
    verdict = gate(db_session, make_action(gate_invoice), now=MIDDAY_IST)
    assert verdict.result_for(CheckName.STOPPING_RULES).passed
    assert "opted_out" not in verdict.result_for(CheckName.STOPPING_RULES).detail["triggers"]


# --- check 4: the rolling window boundary (C3) ---


def _touch_at(db: Session, gate_invoice: Invoice, when: datetime) -> None:
    """One executed outbound action at an exact instant."""
    db.execute(
        text(
            "INSERT INTO actions (id, merchant_id, invoice_id, type, status, proposed_by, "
            "rationale, scheduled_for, executed_at) VALUES "
            "(:id, :m, :i, 'send_message', 'executed', 'agent', 'test', :t, :t)"
        ),
        {
            "id": uuid.uuid4(),
            "m": gate_invoice.merchant_id,
            "i": gate_invoice.id,
            "t": when,
        },
    )
    db.flush()


def test_a_touch_just_inside_seven_days_counts(
    db_session: Session, gate_invoice: Invoice, gate_consent: Consent
) -> None:
    """6 days 23 hours ago is inside the window."""
    for _ in range(3):
        _touch_at(db_session, gate_invoice, MIDDAY_IST - timedelta(days=6, hours=23))
    verdict = gate(db_session, make_action(gate_invoice), now=MIDDAY_IST)
    result = verdict.result_for(CheckName.FREQUENCY_CAP)
    assert result.detail["touches_this_week"] == 3
    assert not result.passed


def test_a_touch_just_outside_seven_days_does_not_count(
    db_session: Session, gate_invoice: Invoice, gate_consent: Consent
) -> None:
    """7 days 1 hour ago is outside it. Date truncation used to let this one in."""
    for _ in range(3):
        _touch_at(db_session, gate_invoice, MIDDAY_IST - timedelta(days=7, hours=1))
    verdict = gate(db_session, make_action(gate_invoice), now=MIDDAY_IST)
    result = verdict.result_for(CheckName.FREQUENCY_CAP)
    assert result.detail["touches_this_week"] == 0
    assert result.passed


def test_the_window_is_rolling_not_calendar(
    db_session: Session, gate_invoice: Invoice, gate_consent: Consent
) -> None:
    """A calendar week would let 2 touches Sunday plus 2 Monday both pass.

    Under a rolling window all four are in scope and the fifth is blocked, which is the point of
    a frequency cap.
    """
    sunday = datetime(2026, 8, 23, 18, 0, tzinfo=IST)  # a Sunday evening
    monday = datetime(2026, 8, 24, 9, 0, tzinfo=IST)  # the next morning, a new calendar week
    for _ in range(2):
        _touch_at(db_session, gate_invoice, sunday)
    for _ in range(2):
        _touch_at(db_session, gate_invoice, monday)

    verdict = gate(db_session, make_action(gate_invoice), now=MIDDAY_IST)
    result = verdict.result_for(CheckName.FREQUENCY_CAP)
    assert result.detail["touches_this_week"] == 4, "a calendar week would have counted 2"
    assert not result.passed


def test_the_window_start_is_reported_in_ist(
    db_session: Session, gate_invoice: Invoice, gate_consent: Consent
) -> None:
    """The detail must say which window was applied, in the timezone it was applied in."""
    verdict = gate(db_session, make_action(gate_invoice), now=MIDDAY_IST)
    start = verdict.result_for(CheckName.FREQUENCY_CAP).detail["window_start_ist"]
    assert start == (MIDDAY_IST - timedelta(days=7)).isoformat()
    assert "+05:30" in start
