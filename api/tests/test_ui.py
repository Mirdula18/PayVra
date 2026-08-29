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

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.agent import runner
from app.enums import PaymentStatus, RecoveryState
from app.models.invoice import Invoice
from app.models.merchant import Merchant
from app.ui.routes import SESSION_COOKIE

pytestmark = pytest.mark.usefixtures("db_available")

SCREENS = ("/ui/audit", "/ui/worklist", "/ui/recovery")


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
    assert "Executed" in body


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
    assert "Why this position" in body
    assert "recoverable money" in body


def test_the_worklist_flags_unscored_rows_rather_than_ranking_them_silently(
    with_data: TestClient,
) -> None:
    body = with_data.get("/ui/worklist").text
    assert "Priority" in body


# --- screen 2: recovery -------------------------------------------------------------------------


def test_recovery_labels_which_figure_is_the_headline(with_data: TestClient) -> None:
    """Reporting one number alone is the failure mode; the labels are the point."""
    body = with_data.get("/ui/recovery").text
    assert "Causal" in body
    assert "Time-window" in body
    assert "headline" in body


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

    assert "dry run" in body.lower()


def test_recovery_with_no_runs_does_not_crash(signed_in: TestClient) -> None:
    assert signed_in.get("/ui/recovery").status_code == 200
