"""HTTP-level tests for the ingestion endpoints, including tenant isolation on every one.

Tenant isolation is the test that matters most here. ``merchant_id`` comes from the auth header
and never from the request, so the check is that merchant B's token cannot reach merchant A's
batch, its repair queue, or its mapping -- and that the failure is a 404, not a 403, because
"this exists but is not yours" leaks the existence of another tenant's data.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import SessionLocal

pytestmark = pytest.mark.usefixtures("db_available")

API = "/api/v1"

CSV = b"""Invoice #,Customer,Amount,Invoice Date,Due Date
INV-API-001,Krishna Textiles,245000,18/02/2026,20/03/2026
INV-API-002,Anand Enterprises,88000,12/02/2026,13/03/2026
INV-API-003,Meridian Logistics LLP,410000,18/01/2026,17/02/2026
"""

MESSY_CSV = b"""Invoice #,Customer,Amount,Invoice Date,Due Date
INV-API-010,Krishna Textiles,245000,18/02/2026,20/03/2026
INV-API-011,Deccan Steel Traders Pvt Ltd,156000,18/02/2026,
"""


def _auth(merchant_id: uuid.UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {merchant_id}"}


def _upload(
    client: TestClient, merchant_id: uuid.UUID, data: bytes = CSV, filename: str = "book.csv"
) -> dict[str, Any]:
    response = client.post(
        f"{API}/batches",
        headers=_auth(merchant_id),
        files={"file": (filename, data, "text/csv")},
        data={"name": "August ledger"},
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture()
def other_merchant() -> Any:
    """A second committed merchant, for the isolation tests."""
    from app.models.merchant import Merchant

    merchant_id = uuid.uuid4()
    session = SessionLocal()
    try:
        session.add(Merchant(id=merchant_id, name="Other", email=f"{merchant_id}@test.local"))
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


# --- POST /batches ----------------------------------------------------------------------------


def test_upload_returns_the_contract_shape(client: TestClient, api_merchant: uuid.UUID) -> None:
    body = _upload(client, api_merchant)
    for key in (
        "batch_id",
        "created",
        "updated",
        "duplicates",
        "repair_queue",
        "counterparties_matched",
        "counterparties_quarantined",
        "column_mapping",
        "total_outstanding_paise",
    ):
        assert key in body, f"missing {key} from the POST /batches contract"
    assert body["created"] == 3
    assert body["repair_queue"] == 0
    assert body["total_outstanding_paise"] == (245000 + 88000 + 410000) * 100
    assert body["column_mapping"]["Invoice #"] == "invoice_number"
    assert body["column_mapping"]["Customer"] == "counterparty_name"


def test_reupload_reports_duplicates_not_new_invoices(
    client: TestClient, api_merchant: uuid.UUID
) -> None:
    _upload(client, api_merchant)
    second = _upload(client, api_merchant)
    assert second["created"] == 0
    assert second["duplicates"] == 3
    assert second["updated"] == 3


def test_unsupported_file_type_is_422_in_the_error_envelope(
    client: TestClient, api_merchant: uuid.UUID
) -> None:
    response = client.post(
        f"{API}/batches",
        headers=_auth(api_merchant),
        files={"file": ("ledger.pdf", b"%PDF-1.4", "application/pdf")},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "FILE_UNREADABLE"
    assert "message" in body["error"] and "details" in body["error"]


def test_empty_file_is_rejected(client: TestClient, api_merchant: uuid.UUID) -> None:
    response = client.post(
        f"{API}/batches",
        headers=_auth(api_merchant),
        files={"file": ("empty.csv", b"", "text/csv")},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


# --- GET /batches/{id}/repairs ----------------------------------------------------------------


def test_repair_queue_lists_failed_rows_with_reasons(
    client: TestClient, api_merchant: uuid.UUID
) -> None:
    body = _upload(client, api_merchant, MESSY_CSV, "messy.csv")
    assert body["repair_queue"] == 1

    response = client.get(f"{API}/batches/{body['batch_id']}/repairs", headers=_auth(api_merchant))
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    item = payload["items"][0]
    assert item["error_code"] == "missing_due_date"
    assert item["raw"]["Invoice #"] == "INV-API-011"
    assert item["row_number"] == 3


def test_repair_queue_is_paginated(client: TestClient, api_merchant: uuid.UUID) -> None:
    body = _upload(client, api_merchant, MESSY_CSV, "messy.csv")
    response = client.get(
        f"{API}/batches/{body['batch_id']}/repairs?limit=1&offset=0",
        headers=_auth(api_merchant),
    )
    assert response.status_code == 200
    assert response.json()["limit"] == 1


# --- POST /batches/{id}/repairs/{row_id} ------------------------------------------------------


def test_submitting_a_correction_creates_the_invoice(
    client: TestClient, api_merchant: uuid.UUID
) -> None:
    body = _upload(client, api_merchant, MESSY_CSV, "messy.csv")
    batch_id = body["batch_id"]
    row = client.get(f"{API}/batches/{batch_id}/repairs", headers=_auth(api_merchant)).json()[
        "items"
    ][0]

    response = client.post(
        f"{API}/batches/{batch_id}/repairs/{row['row_id']}",
        headers=_auth(api_merchant),
        json={"values": {"Due Date": "2026-03-20"}},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "repaired"
    assert result["invoice_id"] is not None
    assert result["remaining_repairs"] == 0
    assert result["batch_status"] == "complete"


def test_a_row_can_be_discarded(client: TestClient, api_merchant: uuid.UUID) -> None:
    body = _upload(client, api_merchant, MESSY_CSV, "messy.csv")
    batch_id = body["batch_id"]
    row = client.get(f"{API}/batches/{batch_id}/repairs", headers=_auth(api_merchant)).json()[
        "items"
    ][0]

    response = client.post(
        f"{API}/batches/{batch_id}/repairs/{row['row_id']}",
        headers=_auth(api_merchant),
        json={"discard": True},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "discarded"
    assert response.json()["remaining_repairs"] == 0


# --- POST /batches/{id}/mapping ---------------------------------------------------------------


def test_mapping_override_reparses_without_reupload(
    client: TestClient, api_merchant: uuid.UUID
) -> None:
    """Headers our dictionary does not know, resolved by the merchant instead."""
    odd = b"""Ref,Buyer Org,Value In INR,Raised,Settle By
INV-API-020,Krishna Textiles,245000,18/02/2026,20/03/2026
"""
    body = _upload(client, api_merchant, odd, "odd.csv")
    # "Value In INR", "Raised" and "Settle By" are not known variants, so required fields are
    # unmapped and the whole batch waits for a mapping.
    assert body["repair_queue"] == 1
    assert body["created"] == 0

    response = client.post(
        f"{API}/batches/{body['batch_id']}/mapping",
        headers=_auth(api_merchant),
        json={
            "column_mapping": {
                "Ref": "invoice_number",
                "Buyer Org": "counterparty_name",
                "Value In INR": "amount",
                "Raised": "issue_date",
                "Settle By": "due_date",
            }
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["created"] == 1
    assert response.json()["repair_queue"] == 0


def test_mapping_override_rejects_an_unknown_canonical_field(
    client: TestClient, api_merchant: uuid.UUID
) -> None:
    body = _upload(client, api_merchant)
    response = client.post(
        f"{API}/batches/{body['batch_id']}/mapping",
        headers=_auth(api_merchant),
        json={"column_mapping": {"Invoice #": "not_a_real_field"}},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


# --- auth -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", f"{API}/batches"),
        ("get", f"{API}/batches/{uuid.uuid4()}/repairs"),
        ("post", f"{API}/batches/{uuid.uuid4()}/repairs/{uuid.uuid4()}"),
        ("post", f"{API}/batches/{uuid.uuid4()}/mapping"),
    ],
)
def test_every_endpoint_requires_auth(client: TestClient, method: str, path: str) -> None:
    response = getattr(client, method)(path)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_a_token_for_a_nonexistent_merchant_fails_closed(client: TestClient) -> None:
    """Not an empty result set -- a 401."""
    response = client.get(f"{API}/batches/{uuid.uuid4()}/repairs", headers=_auth(uuid.uuid4()))
    assert response.status_code == 401


def test_malformed_token_is_rejected(client: TestClient) -> None:
    response = client.get(
        f"{API}/batches/{uuid.uuid4()}/repairs", headers={"Authorization": "Bearer nonsense"}
    )
    assert response.status_code == 401


# --- tenant isolation -------------------------------------------------------------------------


def test_another_merchant_cannot_read_the_repair_queue(
    client: TestClient, api_merchant: uuid.UUID, other_merchant: uuid.UUID
) -> None:
    body = _upload(client, api_merchant, MESSY_CSV, "messy.csv")
    response = client.get(
        f"{API}/batches/{body['batch_id']}/repairs", headers=_auth(other_merchant)
    )
    assert response.status_code == 404, "cross-tenant read must not succeed"
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_another_merchant_cannot_submit_a_repair(
    client: TestClient, api_merchant: uuid.UUID, other_merchant: uuid.UUID
) -> None:
    body = _upload(client, api_merchant, MESSY_CSV, "messy.csv")
    batch_id = body["batch_id"]
    row = client.get(f"{API}/batches/{batch_id}/repairs", headers=_auth(api_merchant)).json()[
        "items"
    ][0]

    response = client.post(
        f"{API}/batches/{batch_id}/repairs/{row['row_id']}",
        headers=_auth(other_merchant),
        json={"values": {"Due Date": "2026-03-20"}},
    )
    assert response.status_code == 404


def test_another_merchant_cannot_override_the_mapping(
    client: TestClient, api_merchant: uuid.UUID, other_merchant: uuid.UUID
) -> None:
    body = _upload(client, api_merchant)
    response = client.post(
        f"{API}/batches/{body['batch_id']}/mapping",
        headers=_auth(other_merchant),
        json={"column_mapping": {"Invoice #": "invoice_number"}},
    )
    assert response.status_code == 404


def test_uploads_from_two_merchants_do_not_mix(
    client: TestClient, api_merchant: uuid.UUID, other_merchant: uuid.UUID
) -> None:
    """The same invoice numbers under two tenants must produce two independent sets."""
    first = _upload(client, api_merchant)
    second = _upload(client, other_merchant)
    assert first["created"] == 3
    # Identical invoice numbers, different merchant: creations, not duplicates.
    assert second["created"] == 3
    assert second["duplicates"] == 0

    session = SessionLocal()
    try:
        count = session.execute(
            text("SELECT count(*) FROM invoices WHERE merchant_id = :m"), {"m": other_merchant}
        ).scalar_one()
        assert count == 3
        session.execute(
            text(
                "DELETE FROM batch_rows WHERE batch_id IN "
                "(SELECT id FROM batches WHERE merchant_id = :m)"
            ),
            {"m": other_merchant},
        )
        session.execute(text("DELETE FROM batches WHERE merchant_id = :m"), {"m": other_merchant})
        session.execute(text("DELETE FROM invoices WHERE merchant_id = :m"), {"m": other_merchant})
        session.execute(
            text(
                "DELETE FROM contacts WHERE counterparty_id IN "
                "(SELECT id FROM counterparties WHERE merchant_id = :m)"
            ),
            {"m": other_merchant},
        )
        session.execute(
            text("DELETE FROM counterparties WHERE merchant_id = :m"), {"m": other_merchant}
        )
        session.commit()
    finally:
        session.close()
