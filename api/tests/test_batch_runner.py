"""The batch runner and run-scoped recovery measurement (FR-16, FR-17).

These tests run against the real gate, the real registry and the real audit log. Only the Razorpay
transport is stubbed, and only in the tests that execute a live-mode run -- everything else uses
``dry_run``, which is itself the behaviour under test.

The property that matters most here is that **a refusal is a result, not an error**: the run keeps
going, the reason is persisted, and the refusal is in the audit trail. A runner that stopped on the
first blocked account would produce a shorter demo and a much weaker compliance story.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import metrics, runner
from app.audit.log import record as audit_record
from app.audit.log import verify_chain
from app.clock import IST
from app.enums import (
    ActionStatus,
    ActorType,
    PaymentStatus,
    RecoveryRunStatus,
    RecoveryState,
)
from app.models.action import Action
from app.models.audit_log import AuditLog
from app.models.consent import Consent
from app.models.invoice import Invoice
from app.models.merchant import Merchant
from app.models.recovery_run import RecoveryRun
from app.razorpay.client import RazorpayClient

pytestmark = pytest.mark.usefixtures("db_available")

MIDDAY_IST = datetime(2026, 8, 24, 12, 0, tzinfo=IST)
NIGHT_IST = datetime(2026, 8, 24, 3, 0, tzinfo=IST)


@pytest.fixture(autouse=True)
def _no_ambient_window_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise any contact-window override inherited from the environment.

    ``CONTACT_WINDOW_OVERRIDE_START``/``_END`` are read from the environment into ``settings``, so
    a developer who exported them to widen a demo run would silently change what these tests
    assert — and the failure would look like a code regression rather than a dirty shell. A test
    that needs an override sets one explicitly; every other test must start from none.
    """
    monkeypatch.setattr(runner.settings, "contact_window_override_start", None)
    monkeypatch.setattr(runner.settings, "contact_window_override_end", None)


@pytest.fixture()
def chaseable(db_session: Session, gate_invoice: Invoice, gate_consent: Consent) -> Invoice:
    """An invoice the runner should be willing and able to act on."""
    gate_invoice.recovery_state = RecoveryState.CHASING.value
    gate_invoice.payment_status = PaymentStatus.UNPAID.value
    gate_invoice.touch_count = 0
    gate_invoice.priority_score = 100_000
    db_session.flush()
    return gate_invoice


def _run(db: Session, merchant: Merchant, **kw: object) -> runner.RunResult:
    kw.setdefault("dry_run", True)
    kw.setdefault("now", MIDDAY_IST)
    result = runner.run(db, merchant.id, **kw)  # type: ignore[arg-type]
    return result


@pytest.fixture()
def stub_razorpay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the Razorpay transport for live-mode runs.

    Without this a non-dry run reaches the real API, because ``.env`` carries working test-mode
    credentials. A unit test must never depend on a third party being up, and must never spend
    link budget.
    """

    def make() -> RazorpayClient:
        def handler(request: httpx.Request) -> httpx.Response:
            link_id = f"plink_{uuid.uuid4().hex[:12]}"
            return httpx.Response(
                200,
                json={
                    "id": link_id,
                    "short_url": f"https://rzp.io/i/{link_id}",
                    "status": "created",
                },
            )

        client = RazorpayClient(key_id="rzp_test_stub", key_secret="s")
        client._client = httpx.Client(
            transport=httpx.MockTransport(handler), base_url="https://api.razorpay.test/v1"
        )
        return client

    monkeypatch.setattr(runner, "RazorpayClient", make)


# --- the run itself -----------------------------------------------------------------------------


def test_a_run_opens_and_closes_a_recovery_run_row(
    db_session: Session, gate_merchant: Merchant, chaseable: Invoice
) -> None:
    result = _run(db_session, gate_merchant)

    row = db_session.get(RecoveryRun, result.recovery_run_id)
    assert row is not None
    assert row.status == RecoveryRunStatus.COMPLETED.value
    assert row.finished_at is not None
    assert row.dry_run is True


def test_every_action_carries_the_run_id(
    db_session: Session, gate_merchant: Merchant, chaseable: Invoice
) -> None:
    """Without this, "across a batch" is a phrase rather than a scope (FR-16.5)."""
    result = _run(db_session, gate_merchant)

    actions = list(
        db_session.execute(
            select(Action).where(Action.recovery_run_id == result.recovery_run_id)
        ).scalars()
    )
    assert actions
    assert all(a.recovery_run_id == result.recovery_run_id for a in actions)


def test_a_dry_run_creates_no_link_and_executes_nothing(
    db_session: Session, gate_merchant: Merchant, chaseable: Invoice
) -> None:
    """FR-16.7: gate for real, persist the verdict, touch nobody."""
    before = chaseable.touch_count
    result = _run(db_session, gate_merchant)

    assert result.dry_run
    actions = list(
        db_session.execute(
            select(Action).where(Action.recovery_run_id == result.recovery_run_id)
        ).scalars()
    )
    assert all(a.status != ActionStatus.EXECUTED.value for a in actions)
    assert all(a.executed_at is None for a in actions)
    db_session.refresh(chaseable)
    assert chaseable.touch_count == before, "a dry run must not count as a touch"


def test_a_dry_run_still_produces_a_full_gate_verdict(
    db_session: Session, gate_merchant: Merchant, chaseable: Invoice
) -> None:
    """The verdict is the thing worth rehearsing; all seven checks must be recorded."""
    result = _run(db_session, gate_merchant)
    action = db_session.execute(
        select(Action).where(Action.recovery_run_id == result.recovery_run_id)
    ).scalars().first()

    assert action is not None
    assert len(action.gate_verdicts) == 7


# --- refusals -----------------------------------------------------------------------------------


def test_an_out_of_hours_run_refuses_rather_than_failing(
    db_session: Session, gate_merchant: Merchant, chaseable: Invoice
) -> None:
    """The constraint that can zero a demo, asserted rather than discovered on stage."""
    result = _run(db_session, gate_merchant, now=NIGHT_IST)

    assert result.refused >= 1
    assert result.errored == 0, "a refusal is a result, not an error"
    refused = [a for a in result.accounts if a.outcome == runner.OUTCOME_REFUSED]
    assert any("time_window" in a.blocked_by for a in refused)


def test_a_refusal_is_persisted_with_its_reason(
    db_session: Session, gate_merchant: Merchant, chaseable: Invoice
) -> None:
    """Clause 3 of the bar: the refusal list is the artefact."""
    result = _run(db_session, gate_merchant, now=NIGHT_IST)

    action = db_session.execute(
        select(Action).where(Action.recovery_run_id == result.recovery_run_id)
    ).scalars().first()
    assert action is not None
    assert action.status == ActionStatus.GATED_FAIL.value
    assert action.gate_failure_reason


def test_a_refusal_does_not_stop_the_run(
    db_session: Session, gate_merchant: Merchant, chaseable: Invoice, gate_counterparty: object
) -> None:
    """One blocked account must not cost the rest of the batch."""
    second = Invoice(
        id=uuid.uuid4(),
        merchant_id=gate_merchant.id,
        counterparty_id=chaseable.counterparty_id,
        invoice_number="INV-GATE-002",
        amount_paise=5_00_000,
        outstanding_paise=5_00_000,
        issue_date=chaseable.issue_date,
        due_date=chaseable.due_date,
        terms_days=30,
        payment_status=PaymentStatus.UNPAID.value,
        recovery_state=RecoveryState.CHASING.value,
        priority_score=90_000,
    )
    db_session.add(second)
    db_session.flush()

    result = _run(db_session, gate_merchant, now=NIGHT_IST, limit=5)
    assert len(result.accounts) == 2


def test_a_dry_run_leaves_nothing_that_looks_like_queued_work(
    db_session: Session, gate_merchant: Merchant, chaseable: Invoice
) -> None:
    """A rehearsal must not inflate the revoked-on-settle count.

    ``gated_pass`` is one of the statuses reconciliation revokes when an invoice settles, and that
    count is shown on stage as "revoked N pending actions". Dry-run rows sitting in it would
    credit the system with cancelling outreach that was never going to fire.
    """
    result = _run(db_session, gate_merchant)

    actions = list(
        db_session.execute(
            select(Action).where(Action.recovery_run_id == result.recovery_run_id)
        ).scalars()
    )
    assert actions
    pending = {ActionStatus.PROPOSED.value, ActionStatus.GATED_PASS.value,
               ActionStatus.AWAITING_APPROVAL.value}
    approved = [a for a in actions if a.status not in {ActionStatus.GATED_FAIL.value}]
    assert all(a.status not in pending for a in approved)


# --- never over-claiming ------------------------------------------------------------------------


def test_an_undelivered_message_is_not_recorded_as_executed(
    db_session: Session, gate_merchant: Merchant, chaseable: Invoice, stub_razorpay: None
) -> None:
    """``guardrails/gate.py``: the audit log may under-claim, it may never over-claim.

    There is no delivery transport yet (FR-10). A run creates a real link, drafts a real message
    and gets a real gate approval -- and then stops. Recording that as ``executed`` would claim a
    send that never happened.
    """
    gate_merchant.approval_value_threshold_paise = 10_00_00_000
    db_session.flush()

    result = runner.run(db_session, gate_merchant.id, dry_run=False, now=MIDDAY_IST, limit=1)

    outbound = [a for a in result.accounts if a.action_type == "send_message"]
    if not outbound:
        pytest.skip("no outbound action was proposed for this fixture")

    assert all(a.outcome == runner.OUTCOME_APPROVED for a in outbound)
    action = db_session.execute(
        select(Action).where(
            Action.recovery_run_id == result.recovery_run_id,
            Action.type == "send_message",
        )
    ).scalars().first()
    assert action is not None
    assert action.status == ActionStatus.GATED_PASS.value
    assert action.executed_at is None


def test_an_undelivered_message_does_not_count_as_a_touch(
    db_session: Session, gate_merchant: Merchant, chaseable: Invoice, stub_razorpay: None
) -> None:
    """touch_count feeds the frequency cap, which counts contacts.

    Inflating it with drafts nobody received would suppress future real outreach on the strength
    of messages that were never sent.
    """
    gate_merchant.approval_value_threshold_paise = 10_00_00_000
    db_session.flush()
    before = chaseable.touch_count

    runner.run(db_session, gate_merchant.id, dry_run=False, now=MIDDAY_IST, limit=1)

    db_session.refresh(chaseable)
    assert chaseable.touch_count == before


def test_a_completed_state_change_is_recorded_as_executed(
    db_session: Session, gate_merchant: Merchant, chaseable: Invoice, stub_razorpay: None
) -> None:
    """The other half of the same rule: what genuinely happened is recorded as having happened."""
    chaseable.touch_count = 99  # exhausts the ladder, so the policy proposes stop
    db_session.flush()

    result = runner.run(db_session, gate_merchant.id, dry_run=False, now=MIDDAY_IST, limit=1)

    assert result.executed == 1
    db_session.refresh(chaseable)
    assert chaseable.recovery_state == RecoveryState.STOPPED.value


# --- the audit trail ----------------------------------------------------------------------------


def test_the_run_is_filterable_in_the_audit_log(
    db_session: Session, gate_merchant: Merchant, chaseable: Invoice
) -> None:
    """Clause 4: one click to this run's trail.

    The id lives inside ``inputs`` because that is what the hash chain covers -- a column of its
    own could be altered without breaking the chain, on exactly the field a judge reads.
    """
    result = _run(db_session, gate_merchant)

    rows = list(
        db_session.execute(
            select(AuditLog).where(
                AuditLog.inputs["recovery_run_id"].astext == str(result.recovery_run_id)
            )
        ).scalars()
    )
    assert rows, "the run wrote no audit entries"


def test_the_audit_chain_survives_a_run(
    db_session: Session, gate_merchant: Merchant, chaseable: Invoice
) -> None:
    _run(db_session, gate_merchant)
    assert verify_chain(db_session, gate_merchant.id)


# --- the contact window override (FR-16.8) ------------------------------------------------------


def test_no_override_configured_leaves_the_window_alone(gate_merchant: Merchant) -> None:
    window, overridden = runner.resolve_contact_window(gate_merchant)
    assert window == (gate_merchant.contact_hour_start, gate_merchant.contact_hour_end)
    assert not overridden


def test_a_half_configured_override_is_ignored_not_half_applied(
    gate_merchant: Merchant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner.settings, "contact_window_override_start", 6)
    monkeypatch.setattr(runner.settings, "contact_window_override_end", None)
    window, overridden = runner.resolve_contact_window(gate_merchant)
    assert not overridden
    assert window == (8, 19)


def test_an_override_only_ever_widens(
    gate_merchant: Merchant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The knob exists to make a demo possible, not to change policy quietly in either direction."""
    monkeypatch.setattr(runner.settings, "contact_window_override_start", 10)
    monkeypatch.setattr(runner.settings, "contact_window_override_end", 17)
    window, _ = runner.resolve_contact_window(gate_merchant)
    assert window == (8, 19), "a narrower override must not narrow the window"


def test_an_invalid_override_is_ignored(
    gate_merchant: Merchant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner.settings, "contact_window_override_start", 20)
    monkeypatch.setattr(runner.settings, "contact_window_override_end", 4)
    _, overridden = runner.resolve_contact_window(gate_merchant)
    assert not overridden


def test_an_active_override_is_written_to_the_audit_log(
    db_session: Session,
    gate_merchant: Merchant,
    chaseable: Invoice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What makes an out-of-window run compliant *by record* rather than by assertion."""
    monkeypatch.setattr(runner.settings, "contact_window_override_start", 0)
    monkeypatch.setattr(runner.settings, "contact_window_override_end", 24)

    result = _run(db_session, gate_merchant, now=NIGHT_IST)

    overrides = list(
        db_session.execute(
            select(AuditLog).where(
                AuditLog.merchant_id == gate_merchant.id,
                AuditLog.action_type == "run.contact_window_override",
                AuditLog.inputs["recovery_run_id"].astext == str(result.recovery_run_id),
            )
        ).scalars()
    )
    assert len(overrides) == 1
    assert "widened" in overrides[0].rationale


def test_the_merchants_window_is_restored_after_the_run(
    db_session: Session,
    gate_merchant: Merchant,
    chaseable: Invoice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An override is scoped to the run that asked for it, or it silently widens every later run."""
    monkeypatch.setattr(runner.settings, "contact_window_override_start", 0)
    monkeypatch.setattr(runner.settings, "contact_window_override_end", 24)

    _run(db_session, gate_merchant, now=NIGHT_IST)

    db_session.refresh(gate_merchant)
    assert (gate_merchant.contact_hour_start, gate_merchant.contact_hour_end) == (8, 19)


def test_the_gate_still_runs_under_an_override(
    db_session: Session,
    gate_merchant: Merchant,
    chaseable: Invoice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """There is no bypass path. The window is a value the check reads, never a rule it skips."""
    monkeypatch.setattr(runner.settings, "contact_window_override_start", 0)
    monkeypatch.setattr(runner.settings, "contact_window_override_end", 24)

    result = _run(db_session, gate_merchant, now=NIGHT_IST)
    action = db_session.execute(
        select(Action).where(Action.recovery_run_id == result.recovery_run_id)
    ).scalars().first()

    assert action is not None
    assert len(action.gate_verdicts) == 7, "all seven checks must still have run"


# --- recovery measurement (FR-17) ---------------------------------------------------------------


def _settlement(db: Session, invoice: Invoice, amount: int, when: datetime) -> None:
    """Write the settlement audit entry reconciliation would write."""
    audit_record(
        db,
        merchant_id=invoice.merchant_id,
        actor=ActorType.SYSTEM,
        actor_id="test",
        action_type=metrics.SETTLE_ACTION_TYPE,
        subject_type="invoice",
        subject_id=invoice.id,
        outcome="executed",
        rationale="test settlement",
        inputs={"amount_paise": amount, "source": "webhook"},
        created_at=when,
    )


def test_recovery_is_zero_for_a_run_that_recovered_nothing(
    db_session: Session, gate_merchant: Merchant, chaseable: Invoice
) -> None:
    result = _run(db_session, gate_merchant)
    rec = metrics.recovery_for_run(db_session, result.recovery_run_id)
    assert rec.causal.rupees_paise == 0
    assert rec.time_window.rupees_paise == 0


def test_money_against_an_untouched_invoice_is_time_window_only(
    db_session: Session, gate_merchant: Merchant, chaseable: Invoice
) -> None:
    """The headline row of the divergence table, and the one worth volunteering.

    The run did not act on this invoice, so counting the payment as recovery would be dishonest.
    """
    result = _run(db_session, gate_merchant, now=NIGHT_IST)  # everything refused, nothing touched
    run_row = db_session.get(RecoveryRun, result.recovery_run_id)
    assert run_row is not None

    _settlement(db_session, chaseable, 10_000, run_row.started_at + timedelta(seconds=1))
    db_session.flush()

    rec = metrics.recovery_for_run(db_session, result.recovery_run_id)
    assert rec.time_window.rupees_paise == 10_000
    assert rec.causal.rupees_paise == 0
    assert rec.diverges


def test_a_partial_payment_counts_as_recovered_money(
    db_session: Session, gate_merchant: Merchant, chaseable: Invoice
) -> None:
    """FR-17: rupees received, not invoices settled.

    Under the ADR-006 ceiling split a large invoice is collected in tranches. Counting only
    settled invoices would report a partly-recovered receivable as zero -- and would make the
    tranche mechanism lower the very figure it was chosen to raise.
    """
    result = _run(db_session, gate_merchant, now=NIGHT_IST)
    run_row = db_session.get(RecoveryRun, result.recovery_run_id)
    assert run_row is not None

    chaseable.outstanding_paise = 5_00_000  # still owing after the tranche
    _settlement(db_session, chaseable, 3_00_000, run_row.started_at + timedelta(seconds=1))
    db_session.flush()

    rec = metrics.recovery_for_run(db_session, result.recovery_run_id)
    assert rec.time_window.rupees_paise == 3_00_000
    assert rec.time_window.invoices_partially_recovered == 1
    assert rec.time_window.invoices_paid_in_full == 0


def test_a_payment_after_the_run_finishes_still_counts_as_causal(
    db_session: Session, gate_merchant: Merchant, chaseable: Invoice, stub_razorpay: None
) -> None:
    """The bug that made clause 1 unverifiable: causal must not be bounded by finished_at.

    A run completes in seconds; a counterparty pays hours later. Bounding causal by the run's own
    end meant it could only ever count money that arrived *during* those few seconds — so it read
    zero no matter what anyone paid.
    """
    gate_merchant.approval_value_threshold_paise = 10_00_00_000
    db_session.flush()

    result = runner.run(db_session, gate_merchant.id, dry_run=False, now=MIDDAY_IST, limit=1)
    run_row = db_session.get(RecoveryRun, result.recovery_run_id)
    assert run_row is not None
    assert run_row.finished_at is not None

    # Paid well after the run closed — the normal case, not an edge case.
    _settlement(
        db_session, chaseable, 12_00_000, run_row.finished_at + timedelta(hours=6)
    )
    chaseable.outstanding_paise = 0
    db_session.flush()

    rec = metrics.recovery_for_run(db_session, result.recovery_run_id)
    assert rec.causal.rupees_paise == 12_00_000, "causal must keep growing after the run ends"
    assert rec.causal.invoices_paid_in_full == 1
    assert rec.time_window.rupees_paise == 0, "time-window closes at finished_at"
    assert rec.diverges


def test_revocation_on_settle_does_not_erase_causal_attribution(
    db_session: Session, gate_merchant: Merchant, chaseable: Invoice, stub_razorpay: None
) -> None:
    """Two correct behaviours that used to cancel each other out.

    Settling an invoice revokes its pending actions — the most important write in the product.
    Causal attribution used to key on ``status IN (executed, gated_pass)``, so that revocation
    flipped the status to ``revoked`` and destroyed the only evidence the run had acted. The
    recovery figure read zero *because* recovery had happened.

    Keying on ``gate_failure_reason`` fixes it: that is written once and no later event rewrites it.
    """
    gate_merchant.approval_value_threshold_paise = 10_00_00_000
    db_session.flush()

    result = runner.run(db_session, gate_merchant.id, dry_run=False, now=MIDDAY_IST, limit=1)
    run_row = db_session.get(RecoveryRun, result.recovery_run_id)
    assert run_row is not None
    assert run_row.finished_at is not None

    # Exactly what reconciliation does on settle.
    action = db_session.execute(
        select(Action).where(Action.recovery_run_id == result.recovery_run_id)
    ).scalars().first()
    assert action is not None
    action.status = ActionStatus.REVOKED.value
    action.revoked_at = run_row.finished_at
    _settlement(
        db_session, chaseable, 31_81_540, run_row.finished_at + timedelta(minutes=7)
    )
    chaseable.outstanding_paise = 0
    db_session.flush()

    rec = metrics.recovery_for_run(db_session, result.recovery_run_id)
    assert rec.causal.rupees_paise == 31_81_540, "revocation must not erase attribution"
    assert rec.causal.invoices_paid_in_full == 1


def test_a_dry_run_claims_no_causal_recovery(
    db_session: Session, gate_merchant: Merchant, chaseable: Invoice
) -> None:
    """A dry run created no link and contacted nobody, so it claims nothing."""
    result = _run(db_session, gate_merchant)
    run_row = db_session.get(RecoveryRun, result.recovery_run_id)
    assert run_row is not None

    _settlement(db_session, chaseable, 5_00_000, run_row.started_at + timedelta(minutes=1))
    db_session.flush()

    rec = metrics.recovery_for_run(db_session, result.recovery_run_id)
    assert rec.causal.rupees_paise == 0
    assert rec.time_window.rupees_paise == 5_00_000


def test_a_refused_action_never_earns_causal_credit(
    db_session: Session, gate_merchant: Merchant, chaseable: Invoice
) -> None:
    """The row worth volunteering: we declined to contact them and they paid anyway."""
    result = _run(db_session, gate_merchant, now=NIGHT_IST)  # everything refused
    run_row = db_session.get(RecoveryRun, result.recovery_run_id)
    assert run_row is not None
    assert result.refused >= 1

    _settlement(db_session, chaseable, 9_00_000, run_row.started_at + timedelta(minutes=1))
    db_session.flush()

    rec = metrics.recovery_for_run(db_session, result.recovery_run_id)
    assert rec.causal.rupees_paise == 0
    assert rec.time_window.rupees_paise == 9_00_000


def test_money_before_the_run_started_is_in_neither_figure(
    db_session: Session, gate_merchant: Merchant, chaseable: Invoice, stub_razorpay: None
) -> None:
    """Unbounded above, not unbounded. A payment that predates the run is not the run's."""
    gate_merchant.approval_value_threshold_paise = 10_00_00_000
    db_session.flush()

    result = runner.run(db_session, gate_merchant.id, dry_run=False, now=MIDDAY_IST, limit=1)
    run_row = db_session.get(RecoveryRun, result.recovery_run_id)
    assert run_row is not None

    _settlement(db_session, chaseable, 7_00_000, run_row.started_at - timedelta(hours=2))
    db_session.flush()

    rec = metrics.recovery_for_run(db_session, result.recovery_run_id)
    assert rec.causal.rupees_paise == 0
    assert rec.time_window.rupees_paise == 0


def test_money_outside_the_run_window_is_not_counted(
    db_session: Session, gate_merchant: Merchant, chaseable: Invoice
) -> None:
    result = _run(db_session, gate_merchant)
    run_row = db_session.get(RecoveryRun, result.recovery_run_id)
    assert run_row is not None

    _settlement(
        db_session, chaseable, 99_999, run_row.started_at - timedelta(days=1)
    )
    db_session.flush()

    rec = metrics.recovery_for_run(db_session, result.recovery_run_id)
    assert rec.time_window.rupees_paise == 0


def test_recovery_for_an_unknown_run_raises(db_session: Session) -> None:
    with pytest.raises(LookupError):
        metrics.recovery_for_run(db_session, uuid.uuid4())


def test_settled_and_stopped_invoices_are_not_picked_up(
    db_session: Session, gate_merchant: Merchant, chaseable: Invoice
) -> None:
    """A run's limit should be spent on accounts it can actually act on."""
    chaseable.recovery_state = RecoveryState.SETTLED.value
    chaseable.payment_status = PaymentStatus.PAID.value
    chaseable.outstanding_paise = 0
    db_session.flush()

    result = _run(db_session, gate_merchant)
    assert result.accounts == []
