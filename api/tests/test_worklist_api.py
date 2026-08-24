"""Worklist endpoints: ranking, merchant overrides, job idempotency, tenant isolation.

Tenant isolation is checked on all four endpoints. ``merchant_id`` comes from the auth header and
never from the request, so a cross-tenant invoice id must be a 404 -- not a 403, which would
confirm the invoice exists.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.clock import today
from app.db import SessionLocal

pytestmark = pytest.mark.usefixtures("db_available")

API = "/api/v1"

CSV = b"""Invoice #,Customer,Amount,Invoice Date,Due Date
INV-WL-001,Krishna Textiles,1400000,18/01/2026,17/02/2026
INV-WL-002,Anand Enterprises,250000,12/02/2026,14/03/2026
INV-WL-003,Meridian Logistics LLP,80000,20/02/2026,22/03/2026
INV-WL-004,Highland Ceramics,450000,05/01/2026,04/02/2026
"""


def _auth(merchant_id: uuid.UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {merchant_id}"}


def _seed_book(client: TestClient, merchant_id: uuid.UUID) -> None:
    response = client.post(
        f"{API}/batches",
        headers=_auth(merchant_id),
        files={"file": ("wl.csv", CSV, "text/csv")},
    )
    assert response.status_code == 201, response.text
    from app.scoring.worklist import rescore

    session = SessionLocal()
    try:
        rescore(session, merchant_id)
        session.commit()
    finally:
        session.close()


def _invoice_ids(merchant_id: uuid.UUID) -> dict[str, uuid.UUID]:
    session = SessionLocal()
    try:
        return {
            row[0]: row[1]
            for row in session.execute(
                text("SELECT invoice_number, id FROM invoices WHERE merchant_id = :m"),
                {"m": merchant_id},
            ).all()
        }
    finally:
        session.close()


@pytest.fixture()
def other_merchant() -> uuid.UUID:
    from app.models.merchant import Merchant

    merchant_id = uuid.uuid4()
    session = SessionLocal()
    try:
        session.add(Merchant(id=merchant_id, name="WLOther", email=f"{merchant_id}@test.local"))
        session.commit()
    finally:
        session.close()
    yield merchant_id
    session = SessionLocal()
    try:
        session.execute(text("DELETE FROM merchants WHERE id = :m"), {"m": merchant_id})
        session.commit()
    finally:
        session.close()


# --- GET /worklist ---------------------------------------------------------------------------


def test_worklist_returns_the_contract_shape(client: TestClient, api_merchant: uuid.UUID) -> None:
    _seed_book(client, api_merchant)
    body = client.get(f"{API}/worklist", headers=_auth(api_merchant)).json()

    assert set(body) >= {"items", "total", "summary"}
    assert set(body["summary"]) == {
        "total_outstanding_paise",
        "overdue_count",
        "high_risk_count",
    }
    item = body["items"][0]
    for key in (
        "invoice_id",
        "invoice_number",
        "counterparty",
        "outstanding_paise",
        "days_past_due",
        "aging_bucket",
        "crosses_msme_45",
        "recovery_state",
        "inferred_cause",
        "collectability_score",
        "priority_score",
        "priority_reason",
        "current_tone_tier",
        "touch_count",
    ):
        assert key in item, f"missing {key} from the GET /worklist contract"
    assert set(item["counterparty"]) == {"id", "name"}


def test_every_row_carries_a_reason(client: TestClient, api_merchant: uuid.UUID) -> None:
    """FR-4.3. Never null, never blank, on any row."""
    _seed_book(client, api_merchant)
    body = client.get(f"{API}/worklist", headers=_auth(api_merchant)).json()
    assert body["items"]
    for item in body["items"]:
        assert item["priority_reason"]
        assert item["priority_reason"].strip()
        assert item["priority_reason"].endswith(".")


def test_worklist_is_ranked_by_priority_not_by_age(
    client: TestClient, api_merchant: uuid.UUID
) -> None:
    """ADR-008 rejects sorting by days past due -- that is the aging report we claim to beat."""
    _seed_book(client, api_merchant)
    items = client.get(f"{API}/worklist", headers=_auth(api_merchant)).json()["items"]

    priorities = [float(i["priority_score"]) for i in items]
    assert priorities == sorted(priorities, reverse=True)

    ages = [i["days_past_due"] for i in items]
    assert ages != sorted(ages, reverse=True), "ranking must not coincide with an age sort"


def test_worklist_excludes_settled_and_stopped(client: TestClient, api_merchant: uuid.UUID) -> None:
    _seed_book(client, api_merchant)
    ids = _invoice_ids(api_merchant)
    session = SessionLocal()
    try:
        session.execute(
            text("UPDATE invoices SET recovery_state = 'settled' WHERE id = :i"),
            {"i": ids["INV-WL-001"]},
        )
        session.commit()
    finally:
        session.close()

    items = client.get(f"{API}/worklist", headers=_auth(api_merchant)).json()["items"]
    assert str(ids["INV-WL-001"]) not in {i["invoice_id"] for i in items}


def test_state_filter(client: TestClient, api_merchant: uuid.UUID) -> None:
    _seed_book(client, api_merchant)
    ids = _invoice_ids(api_merchant)
    session = SessionLocal()
    try:
        session.execute(
            text("UPDATE invoices SET recovery_state = 'chasing' WHERE id = :i"),
            {"i": ids["INV-WL-002"]},
        )
        session.commit()
    finally:
        session.close()

    items = client.get(f"{API}/worklist?state=chasing", headers=_auth(api_merchant)).json()["items"]
    assert {i["invoice_id"] for i in items} == {str(ids["INV-WL-002"])}


def test_a_terminal_state_is_rejected(client: TestClient, api_merchant: uuid.UUID) -> None:
    response = client.get(f"{API}/worklist?state=settled", headers=_auth(api_merchant))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


# --- pin / snooze / exclude ------------------------------------------------------------------


def test_pin_floats_a_row_to_the_top(client: TestClient, api_merchant: uuid.UUID) -> None:
    _seed_book(client, api_merchant)
    ids = _invoice_ids(api_merchant)
    # INV-WL-003 is the smallest invoice, so it ranks last on merit.
    target = ids["INV-WL-003"]
    before = client.get(f"{API}/worklist", headers=_auth(api_merchant)).json()["items"]
    assert before[0]["invoice_id"] != str(target)

    assert (
        client.post(f"{API}/worklist/{target}/pin", headers=_auth(api_merchant)).status_code == 200
    )
    after = client.get(f"{API}/worklist", headers=_auth(api_merchant)).json()["items"]
    assert after[0]["invoice_id"] == str(target)
    assert after[0]["is_pinned"] is True


def test_snooze_hides_a_row_until_its_date(client: TestClient, api_merchant: uuid.UUID) -> None:
    _seed_book(client, api_merchant)
    ids = _invoice_ids(api_merchant)
    target = ids["INV-WL-001"]
    until = today() + timedelta(days=7)

    response = client.post(
        f"{API}/worklist/{target}/snooze",
        headers=_auth(api_merchant),
        json={"until": until.isoformat()},
    )
    assert response.status_code == 200
    assert response.json()["snoozed_until"] == until.isoformat()

    items = client.get(f"{API}/worklist", headers=_auth(api_merchant)).json()["items"]
    assert str(target) not in {i["invoice_id"] for i in items}


def test_a_past_snooze_date_is_rejected(client: TestClient, api_merchant: uuid.UUID) -> None:
    _seed_book(client, api_merchant)
    target = next(iter(_invoice_ids(api_merchant).values()))
    response = client.post(
        f"{API}/worklist/{target}/snooze",
        headers=_auth(api_merchant),
        json={"until": (today() - timedelta(days=1)).isoformat()},
    )
    assert response.status_code == 422


def test_pinning_lifts_a_snooze(client: TestClient, api_merchant: uuid.UUID) -> None:
    """Asking for a row to lead the list while it is hidden is a contradiction."""
    _seed_book(client, api_merchant)
    target = next(iter(_invoice_ids(api_merchant).values()))
    client.post(
        f"{API}/worklist/{target}/snooze",
        headers=_auth(api_merchant),
        json={"until": (today() + timedelta(days=5)).isoformat()},
    )
    body = client.post(f"{API}/worklist/{target}/pin", headers=_auth(api_merchant)).json()
    assert body["is_pinned"] is True
    assert body["snoozed_until"] is None


def test_exclude_is_terminal(client: TestClient, api_merchant: uuid.UUID) -> None:
    """CLAUDE.md invariant 8: stopping rules are absolute."""
    _seed_book(client, api_merchant)
    target = next(iter(_invoice_ids(api_merchant).values()))
    body = client.post(f"{API}/worklist/{target}/exclude", headers=_auth(api_merchant)).json()
    assert body["recovery_state"] == "stopped"
    assert body["stop_reason"] == "merchant_excluded"

    items = client.get(f"{API}/worklist", headers=_auth(api_merchant)).json()["items"]
    assert str(target) not in {i["invoice_id"] for i in items}


# --- rescore job -----------------------------------------------------------------------------


def test_rescore_is_idempotent(client: TestClient, api_merchant: uuid.UUID) -> None:
    """Run twice, change zero rows -- and write zero duplicate audit entries."""
    from app.scoring.worklist import rescore

    _seed_book(client, api_merchant)
    session = SessionLocal()
    try:
        assert rescore(session, api_merchant) == 0, "the seed helper already scored the book"
        session.commit()

        audit_before = session.execute(
            text("SELECT count(*) FROM audit_log WHERE merchant_id = :m"), {"m": api_merchant}
        ).scalar_one()
        assert rescore(session, api_merchant) == 0
        session.commit()
        audit_after = session.execute(
            text("SELECT count(*) FROM audit_log WHERE merchant_id = :m"), {"m": api_merchant}
        ).scalar_one()
        assert audit_after == audit_before, "an unchanged rescore must not pad the training set"
    finally:
        session.close()


def test_every_score_is_logged_with_its_feature_vector(
    client: TestClient, api_merchant: uuid.UUID
) -> None:
    """ADR-008: that log *is* the LightGBM training set. Start collecting on day one."""
    _seed_book(client, api_merchant)
    session = SessionLocal()
    try:
        rows = session.execute(
            text(
                "SELECT inputs, rationale FROM audit_log "
                "WHERE merchant_id = :m AND action_type = 'score.invoice'"
            ),
            {"m": api_merchant},
        ).all()
    finally:
        session.close()

    assert len(rows) == 4, "one audit entry per scored invoice"
    inputs, rationale = rows[0]
    assert rationale, "the reason string is logged alongside the vector"
    assert set(inputs) >= {"features", "weights", "p_collectable", "priority_score"}
    vector = inputs["features"]
    for name in (
        "payment_reliability",
        "broken_promise_count",
        "engagement_rate",
        "has_dispute",
        "days_past_due",
        "lifetime_revenue",
        "touch_count",
        "amount_at_risk",
        "exposure_share",
    ):
        assert name in vector, f"{name} missing from the logged vector"
    # Raw values too -- a training set built only on this version of the normalisation would be
    # worthless the moment the normalisation changes.
    assert "raw_days_past_due" in vector
    assert "raw_outstanding_paise" in vector


def test_rescore_job_registered_at_0100_daily() -> None:
    from apscheduler.schedulers.background import BackgroundScheduler

    from app.scheduler.registry import RESCORE_WORKLIST_JOB_ID, register_jobs

    scheduler = BackgroundScheduler(jobstores={"default": {"type": "memory"}})
    register_jobs(scheduler)
    job = scheduler.get_job(RESCORE_WORKLIST_JOB_ID)

    assert job is not None
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["hour"] == "1"
    assert fields["minute"] == "0"
    assert job.func_ref == "app.scheduler.jobs:rescore_worklist_all"


def test_rescore_job_takes_a_plain_merchant_id() -> None:
    import inspect

    from app.scheduler.jobs import rescore_worklist

    assert list(inspect.signature(rescore_worklist).parameters) == ["merchant_id"]


# --- tenant isolation, all four endpoints ------------------------------------------------------


def test_worklist_is_scoped_to_the_caller(
    client: TestClient, api_merchant: uuid.UUID, other_merchant: uuid.UUID
) -> None:
    _seed_book(client, api_merchant)
    items = client.get(f"{API}/worklist", headers=_auth(other_merchant)).json()["items"]
    assert items == [], "another merchant must see none of this book"


@pytest.mark.parametrize("action", ["pin", "exclude"])
def test_another_merchant_cannot_act_on_an_invoice(
    client: TestClient, api_merchant: uuid.UUID, other_merchant: uuid.UUID, action: str
) -> None:
    _seed_book(client, api_merchant)
    target = next(iter(_invoice_ids(api_merchant).values()))
    response = client.post(f"{API}/worklist/{target}/{action}", headers=_auth(other_merchant))
    assert response.status_code == 404, "cross-tenant write must not succeed"
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_another_merchant_cannot_snooze_an_invoice(
    client: TestClient, api_merchant: uuid.UUID, other_merchant: uuid.UUID
) -> None:
    _seed_book(client, api_merchant)
    target = next(iter(_invoice_ids(api_merchant).values()))
    response = client.post(
        f"{API}/worklist/{target}/snooze",
        headers=_auth(other_merchant),
        json={"until": (today() + timedelta(days=3)).isoformat()},
    )
    assert response.status_code == 404


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", f"{API}/worklist"),
        ("post", f"{API}/worklist/{uuid.uuid4()}/pin"),
        ("post", f"{API}/worklist/{uuid.uuid4()}/snooze"),
        ("post", f"{API}/worklist/{uuid.uuid4()}/exclude"),
    ],
)
def test_every_worklist_endpoint_requires_auth(client: TestClient, method: str, path: str) -> None:
    response = getattr(client, method)(path)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_rescore_does_not_touch_another_merchants_invoices(
    client: TestClient, api_merchant: uuid.UUID, other_merchant: uuid.UUID
) -> None:
    from app.scoring.worklist import rescore

    _seed_book(client, api_merchant)
    session = SessionLocal()
    try:
        assert rescore(session, other_merchant) == 0
        still_scored = session.execute(
            text(
                "SELECT count(*) FROM invoices "
                "WHERE merchant_id = :m AND priority_score IS NOT NULL"
            ),
            {"m": api_merchant},
        ).scalar_one()
        assert still_scored == 4
    finally:
        session.close()
