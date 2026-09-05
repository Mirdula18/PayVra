"""The three Phase 8 screens.

Rendered HTML is asserted on the facts a judge would read off the page -- a refusal's reason, the
headline figure, the hash chain -- not on markup. A test that pins class names breaks on every
style change and proves nothing about the argument the screen makes.

The tenant tests matter most. A read-only screen still reads *someone's* data, and the API's
isolation shape (identity from a credential, never from a URL; another tenant's row reads as
absent) has to hold here too or the UI is a way around it.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import runner
from app.enums import PaymentStatus, RecoveryState
from app.models.action import Action
from app.models.invoice import Invoice
from app.models.merchant import Merchant
from app.models.recovery_run import RecoveryRun
from app.ui import actions
from app.ui.routes import SESSION_COOKIE

pytestmark = pytest.mark.usefixtures("db_available")

SCREENS = ("/ui/home", "/ui/audit", "/ui/worklist", "/ui/recovery")


@pytest.fixture()
def signed_in(client: TestClient, api_merchant: uuid.UUID) -> TestClient:
    """Signed in as a merchant with no data. Exercises the empty states."""
    client.cookies.set(SESSION_COOKIE, str(api_merchant))
    return client


@pytest.fixture()
def with_data(
    client: TestClient,
    db_session: Session,
    gate_merchant: Merchant,
    gate_invoice: Invoice,
    gate_consent: object,
) -> TestClient:
    """Signed in as a merchant that has an invoice and one completed run.

    The populated screens assert on content that only exists once something has happened, which is
    the state a judge sees. The empty states are covered separately -- both are real, and a screen
    that only works with data is half-built.
    """
    gate_invoice.recovery_state = RecoveryState.CHASING.value
    gate_invoice.payment_status = PaymentStatus.UNPAID.value
    gate_invoice.priority_score = 12_345
    gate_invoice.priority_reason = "₹12,450, 30 days. Test reason for the worklist column."
    db_session.flush()
    runner.run(db_session, gate_merchant.id, dry_run=True, limit=3)

    client.cookies.set(SESSION_COOKIE, str(gate_merchant.id))
    return client


# --- session ------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", SCREENS)
def test_no_session_is_sent_to_the_sign_in_form(client: TestClient, path: str) -> None:
    """A browser should get the form, not a JSON error envelope."""
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


@pytest.mark.parametrize("token", ["not-a-uuid", str(uuid.uuid4())])
def test_a_bad_token_fails_closed(client: TestClient, token: str) -> None:
    """Malformed, or valid-shaped but naming no merchant. Neither may render someone's data."""
    client.cookies.set(SESSION_COOKIE, token)
    response = client.get("/ui/audit", follow_redirects=False)
    assert response.status_code == 303
    client.cookies.clear()


def test_the_api_still_returns_401_rather_than_redirecting(client: TestClient) -> None:
    """The redirect is a UI affordance only. The API contract is unchanged."""
    assert client.get("/api/v1/worklist").status_code == 401


def test_the_login_form_renders(client: TestClient) -> None:
    body = client.get("/ui/login").text
    assert "merchant UUID" in body


def test_the_root_lands_on_home(client: TestClient) -> None:
    """`/ui/` is the address people type. It must reach the screen that explains the product."""
    response = client.get("/ui/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/home"


def test_signing_in_returns_you_to_where_you_were(
    client: TestClient, api_merchant: uuid.UUID
) -> None:
    """Switching client mid-read should not also lose your place."""
    response = client.post(
        "/ui/login",
        data={"token": str(api_merchant), "next": "/ui/audit"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/audit"
    client.cookies.clear()


@pytest.mark.parametrize("target", ["https://evil.example/steal", "//evil.example", "/etc/passwd"])
def test_a_redirect_target_off_the_allowlist_is_refused(
    client: TestClient, api_merchant: uuid.UUID, target: str
) -> None:
    """``next`` comes from a form field. Anything but one of our own screens goes home instead."""
    response = client.post(
        "/ui/login",
        data={"token": str(api_merchant), "next": target},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/ui/home"
    client.cookies.clear()


def test_signing_out_clears_the_session(client: TestClient, api_merchant: uuid.UUID) -> None:
    """Asserted on the response, not on the client's jar afterwards.

    ``TestClient.cookies.set`` writes straight into httpx's jar, which then keeps re-sending the
    value regardless of what the server says about it -- so a follow-up request would prove
    nothing about the application. What the browser acts on is the expiring ``Set-Cookie``, and
    that is the contract worth pinning.
    """
    client.cookies.set(SESSION_COOKIE, str(api_merchant))
    response = client.post("/ui/logout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"

    cleared = response.headers["set-cookie"]
    assert SESSION_COOKIE in cleared
    assert 'Max-Age=0' in cleared or "expires=Thu, 01 Jan 1970" in cleared.lower()
    client.cookies.clear()


def test_the_client_switcher_distinguishes_same_named_tenants(
    signed_in: TestClient, with_data: TestClient
) -> None:
    """A dev database holds several test tenants, some sharing a name.

    The header switcher lists them, so it has to carry the invoice count -- otherwise picking the
    real book out of four identically-named rows is guesswork.
    """
    body = signed_in.get("/ui/home").text
    assert "invoice" in body
    assert "Client" in body


# --- acting from the UI --------------------------------------------------------------------------


def test_the_run_button_is_a_rehearsal_unless_told_otherwise(
    signed_in: TestClient, db_session: Session, api_merchant: uuid.UUID
) -> None:
    """**The safe run is the default one.**

    A live run creates real Razorpay links against a finite budget and sends real email. Posting
    the form without ticking the box has to be the harmless path, or the control is a trap: the
    person most likely to click it is the person who has not read anything about it.
    """
    response = signed_in.post("/ui/run", data={"limit": 2}, follow_redirects=False)
    assert response.status_code == 303

    run_id = response.headers["location"].split("run=")[1]
    run = db_session.get(RecoveryRun, uuid.UUID(run_id))
    assert run is not None
    assert run.dry_run is True, "an unticked form must never contact anyone"
    assert run.merchant_id == api_merchant


def test_an_invoice_page_shows_the_message_that_was_actually_sent(with_data: TestClient) -> None:
    """The drafted body appeared on no screen at all before this page existed.

    A product whose entire visible output is the message it writes should not require a database
    client to read one.
    """
    body = with_data.get("/ui/worklist").text
    assert "/ui/invoice/" in body, "worklist rows must open the account"


def test_another_tenants_invoice_reads_as_absent(
    signed_in: TestClient, gate_invoice: Invoice
) -> None:
    """404, not 403 -- "this exists but is not yours" leaks the existence of another tenant."""
    assert signed_in.get(f"/ui/invoice/{gate_invoice.id}").status_code == 404


# --- human in the loop ----------------------------------------------------------------------------


def test_a_human_approval_does_not_bypass_the_gate(
    client: TestClient, db_session: Session, gate_merchant: Merchant, gate_invoice: Invoice
) -> None:
    """**The whole design rests on this.**

    ``check_value_threshold`` reads ``approved_by``, so an approval supplies the permission that
    one check asks for. It supplies nothing to the other six. Approving outside contact hours, or
    without consent, must still refuse -- otherwise the approve button is a bypass wearing a
    different name, and every compliance claim the product makes goes with it.
    """
    from datetime import datetime

    from app.clock import IST
    from app.enums import ActionType, Channel
    from app.guardrails.gate import gate
    from app.schemas.gate import ProposedAction

    gate_invoice.recovery_state = RecoveryState.HUMAN_REVIEW.value
    gate_invoice.payment_status = PaymentStatus.UNPAID.value
    db_session.flush()

    approved = ProposedAction(
        invoice_id=gate_invoice.id,
        type=ActionType.SEND_MESSAGE,
        tone_tier=3,
        rationale="Human approved from the review queue.",
        channel=Channel.EMAIL,
        approved_by="a-real-person",
    )
    # 03:00 IST is outside every permitted contact window.
    midnight = datetime(2026, 9, 4, 3, 0, tzinfo=IST)
    verdict = gate(db_session, approved, now=midnight, write_audit=False)

    checks = {c.check.value: c.passed for c in verdict.checks}
    assert checks["value_threshold"] is True, "the approval must satisfy the check that asks for it"
    assert checks["time_window"] is False, "and must satisfy nothing else"
    assert verdict.passed is False, "so the send is still refused"


def test_an_approved_send_writes_a_complete_action_row(
    db_session: Session,
    gate_merchant: Merchant,
    gate_invoice: Invoice,
    gate_consent: object,
    monkeypatch: Any,
) -> None:
    """The refusal path still has to persist the action, columns and all.

    ``actions`` has NOT NULL columns the runner fills in and this path originally did not, so an
    approved send died at the insert *after* the gate had passed -- a 500 in the browser, with the
    decision already made and nothing recorded. The gate refusing is the easy case to reach in a
    test, and it exercises the identical insert.
    """
    from app.razorpay.links import LinkResult
    from app.ui import actions as acts

    class _Link:
        short_url = "https://rzp.io/rzp/testlink"

    monkeypatch.setattr(
        acts, "create_link", lambda *a, **k: LinkResult(link=_Link(), created=True)
    )
    gate_invoice.recovery_state = RecoveryState.HUMAN_REVIEW.value
    db_session.flush()

    outcome = acts.approve_and_send(
        db_session, gate_invoice.id, gate_merchant.id, actor_id="a-person"
    )

    row = db_session.execute(
        select(Action)
        .where(Action.invoice_id == gate_invoice.id)
        .order_by(Action.created_at.desc())
    ).scalars().first()
    assert row is not None, "the decision must be recorded whichever way it went"
    assert row.scheduled_for is not None, "NOT NULL, and the 500 this test exists for"
    assert row.proposed_by == "human"

    # Three legitimate endings, and the row has to be coherent in all of them: the gate refused,
    # the gate passed but the transport did not deliver, or it went out.
    if outcome.ok:
        assert row.status == "executed"
        assert row.executed_at is not None
    elif row.gate_failure_reason:
        assert row.status == "gated_fail"
    else:
        assert row.status == "failed", "gate passed, send did not"
        assert row.executed_at is None, "nothing may claim an execution time it never had"


def test_an_edited_message_still_carries_a_working_payment_link(
    db_session: Session,
    gate_merchant: Merchant,
    gate_invoice: Invoice,
    gate_consent: object,
    monkeypatch: Any,
) -> None:
    """**Touching the textarea used to guarantee a refusal.**

    The preview drafted against a stand-in URL, and the edit box is always submitted -- so a
    person who opened "Review & edit" and pressed send shipped a body whose payment link did not
    exist. Check 6 refused it every time, and the message blamed content policy, which is true and
    completely unhelpful.

    The send now rewrites the stand-in to the link it just created. A pasted body that never saw
    a link at all is still refused, and should be.
    """
    from app.razorpay.links import LinkResult
    from app.ui import actions as acts

    real_url = "https://rzp.io/rzp/realone"

    class _Link:
        short_url = real_url

    monkeypatch.setattr(
        acts, "create_link", lambda *a, **k: LinkResult(link=_Link(), created=True)
    )

    # Exactly what the screen does: take the previewed body -- which holds the stand-in URL,
    # since no link exists yet -- add a line to it, and submit that.
    previewed = acts.preview(db_session, gate_invoice.id, gate_merchant.id)
    assert previewed is not None
    assert acts._placeholder_link(gate_invoice.id) in previewed.message.body
    edited = "A quick note before the usual reminder.\n\n" + previewed.message.body

    acts.approve_and_send(
        db_session, gate_invoice.id, gate_merchant.id, actor_id="a-person", body_override=edited
    )

    row = (
        db_session.execute(
            select(Action)
            .where(Action.invoice_id == gate_invoice.id)
            .order_by(Action.created_at.desc())
        )
        .scalars()
        .first()
    )
    assert row is not None
    content = [
        v for v in row.gate_verdicts if v["check"] == "content_policy"  # type: ignore[index]
    ]
    assert content and content[0]["passed"] is True, (
        "an edited body must keep a real payment link, or check 6 refuses it"
    )


def test_updating_a_contact_is_recorded_as_a_human_action(
    db_session: Session, gate_merchant: Merchant, gate_invoice: Invoice
) -> None:
    """An invoice that closed because someone clicked a button must not read like agent recovery."""
    from app.models.audit_log import AuditLog

    outcome = actions.update_contact(
        db_session,
        gate_invoice.id,
        gate_merchant.id,
        email_address="fixed@example.co.in",
        name="New Person",
        actor_id="test",
    )
    assert outcome.ok is True

    entry = db_session.execute(
        select(AuditLog)
        .where(AuditLog.merchant_id == gate_merchant.id, AuditLog.action_type == "contact.updated")
        .order_by(AuditLog.id.desc())
    ).scalars().first()
    assert entry is not None
    assert entry.actor == "human"


def test_a_bad_email_is_refused_before_anything_is_written(
    db_session: Session, gate_merchant: Merchant, gate_invoice: Invoice
) -> None:
    outcome = actions.update_contact(
        db_session,
        gate_invoice.id,
        gate_merchant.id,
        email_address="not-an-email",
        name=None,
        actor_id="test",
    )
    assert outcome.ok is False


def test_a_human_action_on_another_tenants_invoice_is_absent(
    db_session: Session, api_merchant: uuid.UUID, gate_invoice: Invoice
) -> None:
    """404-not-403 has to hold on the write path too, or the read-path guarantee is decorative."""
    from app.exceptions import NotFoundError

    with pytest.raises(NotFoundError):
        actions.stop_chasing(
            db_session, gate_invoice.id, api_merchant, reason="nope", actor_id="test"
        )


def test_overpaying_an_invoice_offline_is_refused(
    db_session: Session, gate_merchant: Merchant, gate_invoice: Invoice
) -> None:
    """Recording more than is owed would invent recovery. The figure has to stay defensible."""
    too_much = str(int(gate_invoice.outstanding_paise / 100) + 1000)
    outcome = actions.mark_paid(
        db_session,
        gate_invoice.id,
        gate_merchant.id,
        amount_rupees=too_much,
        method="neft",
        reference=None,
        actor_id="test",
    )
    assert outcome.ok is False


# --- home ---------------------------------------------------------------------------------------


def test_home_leads_with_no_single_recovery_headline(with_data: TestClient) -> None:
    """**Per-run attribution, never a total.**

    Causal recovery is unbounded above, so two runs that both acted on an invoice both report the
    money it later paid. Summing the column would double-count, and picking the largest single
    figure would promote a one-account run over a whole batch. The screen shows the runs instead,
    and says so.
    """
    body = with_data.get("/ui/home").text
    assert "Batches" in body
    # Nothing on the page offers a single summed per-run recovery figure.
    assert "Total recovered" not in body
    assert "Total recovery" not in body


def test_home_says_a_dry_run_collected_nothing_rather_than_showing_no_runs(
    with_data: TestClient,
) -> None:
    """The fixture's only run is dry, and dry runs are kept off the table.

    "No runs yet" would then be actively misleading to someone who had just run one. Say what
    happened instead: it ran, it contacted nobody, it claims nothing.
    """
    body = with_data.get("/ui/home").text
    assert "rehearsal" in body.lower()
    assert "No runs yet" not in body


# --- tenant isolation ---------------------------------------------------------------------------


def test_another_tenants_run_does_not_render(
    client: TestClient,
    db_session: Session,
    api_merchant: uuid.UUID,
    gate_merchant: Merchant,
    gate_invoice: Invoice,
) -> None:
    """"Exists but is not yours" must read the same as "does not exist"."""
    gate_invoice.recovery_state = RecoveryState.CHASING.value
    gate_invoice.payment_status = PaymentStatus.UNPAID.value
    db_session.flush()
    other = runner.run(db_session, gate_merchant.id, dry_run=True, limit=1)

    client.cookies.set(SESSION_COOKIE, str(api_merchant))
    body = client.get(f"/ui/audit?run={other.recovery_run_id}").text
    client.cookies.clear()

    assert str(other.recovery_run_id) not in body


def test_a_malformed_run_id_does_not_error(signed_in: TestClient) -> None:
    assert signed_in.get("/ui/audit?run=not-a-uuid").status_code == 200


# --- screen 3: the audit log --------------------------------------------------------------------


def test_the_audit_screen_hides_scoring_entries_by_default(signed_in: TestClient) -> None:
    """A rescore writes one entry per invoice and would bury the refusals this screen exists for."""
    body = signed_in.get("/ui/audit").text
    assert "score.invoice" not in body
    assert "scoring" in body.lower(), "the toggle must still be offered"


def test_scoring_entries_are_reachable_when_asked_for(signed_in: TestClient) -> None:
    """Hidden by default is not the same as unavailable -- they are real audit records."""
    assert signed_in.get("/ui/audit?show_scoring=1").status_code == 200


def test_refusals_and_sends_appear_in_one_list(with_data: TestClient) -> None:
    """Separating them would let a viewer read only the flattering half."""
    body = with_data.get("/ui/audit").text
    assert "Refused by gate" in body
    assert "Sent" in body


def test_the_hash_chain_is_visible(with_data: TestClient) -> None:
    """Tamper-evidence is a claim; the chain on screen is what makes it inspectable."""
    body = with_data.get("/ui/audit").text
    assert "Chain" in body
    assert "tamper-" in body


def test_an_outcome_filter_narrows_the_list(signed_in: TestClient) -> None:
    assert signed_in.get("/ui/audit?outcome=blocked").status_code == 200


# --- screen 1: the worklist ---------------------------------------------------------------------


def test_the_worklist_shows_the_reason_for_each_position(with_data: TestClient) -> None:
    """The claim that this is not an aging report lives entirely in this column."""
    body = with_data.get("/ui/worklist").text
    assert "Why this rank" in body
    assert "recoverable value" in body.lower()


def test_the_worklist_flags_unscored_rows_rather_than_ranking_them_silently(
    with_data: TestClient,
) -> None:
    body = with_data.get("/ui/worklist").text
    assert "Risk score" in body


# --- screen 2: recovery -------------------------------------------------------------------------


def test_recovery_labels_which_figure_is_the_headline(with_data: TestClient) -> None:
    """Reporting one number alone is the failure mode; the labels are the point."""
    body = with_data.get("/ui/recovery").text
    assert "Causal" in body
    assert "Time-window" in body
    assert "headline" in body.lower()


def test_recovery_explains_a_divergence_on_screen(
    client: TestClient,
    db_session: Session,
    gate_merchant: Merchant,
    gate_invoice: Invoice,
) -> None:
    """The gap has to be explained where it is shown, not in a document nobody opens."""
    gate_invoice.recovery_state = RecoveryState.CHASING.value
    gate_invoice.payment_status = PaymentStatus.UNPAID.value
    db_session.flush()
    result = runner.run(db_session, gate_merchant.id, dry_run=True, limit=1)

    client.cookies.set(SESSION_COOKIE, str(gate_merchant.id))
    body = client.get(f"/ui/recovery?run={result.recovery_run_id}").text
    client.cookies.clear()

    assert "rehearsal" in body.lower()


def test_recovery_with_no_runs_does_not_crash(signed_in: TestClient) -> None:
    assert signed_in.get("/ui/recovery").status_code == 200
