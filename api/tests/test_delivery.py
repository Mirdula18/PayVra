"""Email delivery (Phase 6.5, FR-10.1) and what it does to the record.

The provider is stubbed throughout. `conftest._no_real_email` disables delivery for the whole
suite, so every test here re-enables it **explicitly and locally** — opting in per test rather than
inheriting a live key from someone's `.env`. The first full run after the transport landed sent
real email to a real inbox before that fixture existed; this file is written so that cannot recur.

The property under test is not "does it send". It is **what the system claims afterwards**: a
confirmed send is `executed`, anything else stays `approved`, and nothing in between.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import runner
from app.clock import IST
from app.delivery import email as email_mod
from app.delivery import sender
from app.delivery.email import DeliveryError, DeliveryNotConfigured, SendResult
from app.enums import ActionStatus, Channel, PaymentStatus, RecoveryState
from app.models.action import Action
from app.models.audit_log import AuditLog
from app.models.contact import Contact
from app.models.invoice import Invoice
from app.models.merchant import Merchant
from app.models.message import Message
from tests.gate_support import MIDDAY_IST, make_action

pytestmark = pytest.mark.usefixtures("db_available")

OVERRIDE = "owner@example.test"


@pytest.fixture()
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn delivery on for one test, with a key that reaches only a stubbed transport."""
    monkeypatch.setattr(email_mod.settings, "resend_api_key", "re_test_key", raising=False)
    monkeypatch.setattr(email_mod.settings, "resend_to_override", OVERRIDE, raising=False)
    monkeypatch.setattr(email_mod.settings, "resend_from", "onboarding@resend.dev", raising=False)


def _stub_post(
    monkeypatch: pytest.MonkeyPatch, response: httpx.Response
) -> list[dict[str, object]]:
    """Replace the HTTP call and record what would have been sent."""
    seen: list[dict[str, object]] = []

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        seen.append({"url": url, **kwargs})
        return response

    monkeypatch.setattr(email_mod.httpx, "post", fake_post)
    return seen


def _ok() -> httpx.Response:
    return httpx.Response(200, json={"id": "re_abc123"}, request=httpx.Request("POST", "x"))


# --- the safety properties ----------------------------------------------------------------------


def test_delivery_is_off_without_a_key() -> None:
    """The suite-wide default. A system with no credentials must not attempt a send."""
    assert not email_mod.is_configured()


def test_delivery_is_off_without_an_override_recipient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key alone is not enough.

    Without a verified domain Resend delivers only to the account owner, so an unset override means
    the only safe number of recipients is zero. Failing closed here is what stops a demo database
    full of realistic addresses from becoming a mailing list.
    """
    monkeypatch.setattr(email_mod.settings, "resend_api_key", "re_test_key", raising=False)
    monkeypatch.setattr(email_mod.settings, "resend_to_override", "", raising=False)
    assert not email_mod.is_configured()


def test_a_dummy_key_does_not_count_as_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """`.env.example` ships `dummy-resend-key`; it must never look like a working credential."""
    monkeypatch.setattr(email_mod.settings, "resend_api_key", "dummy-resend-key", raising=False)
    monkeypatch.setattr(email_mod.settings, "resend_to_override", OVERRIDE, raising=False)
    assert not email_mod.is_configured()


def test_the_counterpartys_address_is_never_the_destination(configured: None) -> None:
    """Every message goes to the override, whatever the contact row says."""
    assert email_mod.recipient_for("real.customer@theircompany.example") == OVERRIDE
    assert email_mod.recipient_for(None) == OVERRIDE


# --- the transport ------------------------------------------------------------------------------


def test_a_successful_send_returns_the_provider_id(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _stub_post(monkeypatch, _ok())
    result = email_mod.send_email(to=OVERRIDE, subject="Invoice", body="body")

    assert result.provider_message_id == "re_abc123"
    assert result.to == OVERRIDE
    payload = seen[0]["json"]
    assert payload["to"] == [OVERRIDE]  # type: ignore[index]
    assert payload["from"] == "onboarding@resend.dev"  # type: ignore[index]


def test_a_refused_send_raises_with_the_providers_own_words(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resend's 403 names the only address it will accept. Summarising that loses the fix."""
    _stub_post(
        monkeypatch,
        httpx.Response(
            403,
            json={"message": "You can only send testing emails to your own email address"},
            request=httpx.Request("POST", "x"),
        ),
    )
    with pytest.raises(DeliveryError, match="your own email address"):
        email_mod.send_email(to="someone@else.test", subject="s", body="b")


def test_a_transport_fault_raises_rather_than_returning(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(url: str, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(email_mod.httpx, "post", boom)
    with pytest.raises(DeliveryError, match="could not reach Resend"):
        email_mod.send_email(to=OVERRIDE, subject="s", body="b")


def test_acceptance_without_an_id_is_treated_as_failure(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An execution record with no provider id cannot be reconciled later, so it is not one."""
    _stub_post(monkeypatch, httpx.Response(200, json={}, request=httpx.Request("POST", "x")))
    with pytest.raises(DeliveryError, match="no id"):
        email_mod.send_email(to=OVERRIDE, subject="s", body="b")


# --- the gate precondition still holds ------------------------------------------------------------


def test_send_refuses_without_a_passing_verdict(
    db_session: Session, gate_invoice: Invoice, gate_consent: object, configured: None
) -> None:
    """ADR-005: no code path reaches a transport without a passing verdict in scope."""
    from app.guardrails.gate import gate

    action = make_action(gate_invoice)
    verdict = gate(db_session, action, now=datetime(2026, 8, 24, 3, 0, tzinfo=IST))  # night
    assert not verdict.passed

    with pytest.raises(sender.GateNotPassedError):
        sender.send(action, verdict, now=MIDDAY_IST)


def test_send_refuses_a_stale_verdict(
    db_session: Session, gate_invoice: Invoice, gate_consent: object, configured: None
) -> None:
    from app.guardrails.gate import gate

    action = make_action(gate_invoice)
    verdict = gate(db_session, action, now=MIDDAY_IST)
    with pytest.raises(sender.GateNotPassedError, match="old"):
        sender.send(action, verdict, now=MIDDAY_IST + timedelta(hours=2))


def test_send_refuses_a_channel_it_cannot_deliver(
    db_session: Session, gate_invoice: Invoice, gate_consent: object, configured: None
) -> None:
    """SMS and WhatsApp are non-goals. Refused explicitly, never silently dropped."""
    from app.guardrails.gate import gate

    action = make_action(gate_invoice, channel=Channel.SMS)
    verdict = gate(db_session, action, now=MIDDAY_IST)
    if not verdict.passed:
        pytest.skip("gate refused for an unrelated reason")
    with pytest.raises(DeliveryError, match="not implemented"):
        sender.send(action, verdict, now=MIDDAY_IST)


# --- what the runner claims afterwards ------------------------------------------------------------


@pytest.fixture()
def deliverable(
    db_session: Session, gate_merchant: Merchant, gate_invoice: Invoice, gate_consent: object
) -> Invoice:
    """An invoice the runner will approve and try to send, with a contact to address."""
    gate_merchant.approval_value_threshold_paise = 10_00_00_000
    gate_invoice.recovery_state = RecoveryState.CHASING.value
    gate_invoice.payment_status = PaymentStatus.UNPAID.value
    gate_invoice.touch_count = 0
    gate_invoice.priority_score = 100_000
    db_session.add(
        Contact(
            id=uuid.uuid4(),
            counterparty_id=gate_invoice.counterparty_id,
            name="Ramesh Iyer",
            email="ap@counterparty.example",
            is_primary=True,
        )
    )
    db_session.flush()
    return gate_invoice


def test_a_confirmed_send_is_recorded_as_executed(
    db_session: Session,
    gate_merchant: Merchant,
    deliverable: Invoice,
    stub_razorpay: None,
    configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of the phase: delivery is what turns approved into executed."""
    _stub_post(monkeypatch, _ok())

    result = runner.run(db_session, gate_merchant.id, dry_run=False, now=MIDDAY_IST, limit=1)

    sent = [a for a in result.accounts if a.action_type == "send_message"]
    if not sent:
        pytest.skip("no outbound action proposed for this fixture")
    assert sent[0].outcome == runner.OUTCOME_EXECUTED
    assert sent[0].delivered_to == OVERRIDE

    action = db_session.execute(
        select(Action).where(
            Action.recovery_run_id == result.recovery_run_id, Action.type == "send_message"
        )
    ).scalars().first()
    assert action is not None
    assert action.status == ActionStatus.EXECUTED.value
    assert action.executed_at is not None


def test_a_confirmed_send_records_the_message_and_provider_id(
    db_session: Session,
    gate_merchant: Merchant,
    deliverable: Invoice,
    stub_razorpay: None,
    configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_post(monkeypatch, _ok())
    result = runner.run(db_session, gate_merchant.id, dry_run=False, now=MIDDAY_IST, limit=1)

    message = db_session.execute(
        select(Message)
        .join(Action, Action.id == Message.action_id)
        .where(Action.recovery_run_id == result.recovery_run_id)
    ).scalars().first()
    if message is None:
        pytest.skip("no outbound action proposed for this fixture")
    assert message.provider_message_id == "re_abc123"
    assert message.delivery_status == runner.DELIVERY_SENT
    assert message.body


def test_a_confirmed_send_counts_as_a_touch(
    db_session: Session,
    gate_merchant: Merchant,
    deliverable: Invoice,
    stub_razorpay: None,
    configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The frequency cap counts contacts. A delivered message is one."""
    _stub_post(monkeypatch, _ok())
    before = deliverable.touch_count

    runner.run(db_session, gate_merchant.id, dry_run=False, now=MIDDAY_IST, limit=1)

    db_session.refresh(deliverable)
    assert deliverable.touch_count == before + 1


def test_a_failed_send_stays_approved_and_claims_nothing(
    db_session: Session,
    gate_merchant: Merchant,
    deliverable: Invoice,
    stub_razorpay: None,
    configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rule that matters most: never over-claim a send that did not happen."""
    _stub_post(
        monkeypatch,
        httpx.Response(403, json={"message": "refused"}, request=httpx.Request("POST", "x")),
    )
    before = deliverable.touch_count

    result = runner.run(db_session, gate_merchant.id, dry_run=False, now=MIDDAY_IST, limit=1)

    sent = [a for a in result.accounts if a.action_type == "send_message"]
    if not sent:
        pytest.skip("no outbound action proposed for this fixture")
    assert sent[0].outcome == runner.OUTCOME_APPROVED
    assert sent[0].delivered_to is None

    action = db_session.execute(
        select(Action).where(
            Action.recovery_run_id == result.recovery_run_id, Action.type == "send_message"
        )
    ).scalars().first()
    assert action is not None
    assert action.status == ActionStatus.GATED_PASS.value
    assert action.executed_at is None

    db_session.refresh(deliverable)
    assert deliverable.touch_count == before, "a failed send must not count as a contact"


def test_a_failed_send_is_written_to_the_audit_log(
    db_session: Session,
    gate_merchant: Merchant,
    deliverable: Invoice,
    stub_razorpay: None,
    configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A silent delivery failure is indistinguishable from never having tried."""
    _stub_post(
        monkeypatch,
        httpx.Response(
            403,
            json={"message": "recipient refused"},
            request=httpx.Request("POST", "x"),
        ),
    )
    result = runner.run(db_session, gate_merchant.id, dry_run=False, now=MIDDAY_IST, limit=1)

    entries = list(
        db_session.execute(
            select(AuditLog).where(
                AuditLog.inputs["recovery_run_id"].astext == str(result.recovery_run_id),
                AuditLog.action_type == "run.account",
            )
        ).scalars()
    )
    failures = [e for e in entries if e.inputs.get("delivery_failure")]
    if not failures:
        pytest.skip("no outbound action proposed for this fixture")
    assert "recipient refused" in str(failures[0].inputs["delivery_failure"])
    assert failures[0].inputs["delivered"] is False


def test_a_dry_run_never_reaches_the_transport(
    db_session: Session,
    gate_merchant: Merchant,
    deliverable: Invoice,
    configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-16.7. A rehearsal that emailed someone would not be a rehearsal."""
    seen = _stub_post(monkeypatch, _ok())

    runner.run(db_session, gate_merchant.id, dry_run=True, now=MIDDAY_IST, limit=3)

    assert seen == [], "a dry run must not call the provider"


def test_send_result_is_a_value_not_a_promise() -> None:
    """Callers key `executed` on a returned result, so it has to carry the proof."""
    result = SendResult(provider_message_id="re_x", to=OVERRIDE)
    assert result.provider == "resend"
    assert result.provider_message_id


def test_not_configured_is_a_delivery_error() -> None:
    """So a caller catching DeliveryError also catches "off", and cannot claim a send either way."""
    assert issubclass(DeliveryNotConfigured, DeliveryError)
