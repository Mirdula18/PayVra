"""The Phase 1 acceptance test: export the seed to CSV, re-upload it, get the right counts.

This is the end-to-end proof that ingestion, mapping, date handling, matching, the duplicate rule
and aging all agree with what Phase 0 built. It runs against the seeded database, so it skips
cleanly when the seed has not been loaded.

The upload goes in under a *fresh* merchant, so the seeded merchant's own data is never mutated:
every seeded invoice number should come back as a creation for the new tenant, which also proves
invoice numbers are scoped per merchant and not globally unique.
"""

from __future__ import annotations

import csv
import io
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import SessionLocal

pytestmark = pytest.mark.usefixtures("db_available")

API = "/api/v1"
FIXTURES = Path(__file__).resolve().parents[1] / "app" / "seed" / "fixtures"
MESSY_FIXTURE = FIXTURES / "messy_upload.csv"
AMBIGUOUS_FIXTURE = FIXTURES / "ambiguous_dates.csv"


def _auth(merchant_id: uuid.UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {merchant_id}"}


def _export_seed_to_csv() -> tuple[bytes, int, int]:
    """Export the seeded merchant's invoices as a CSV a merchant might actually upload.

    Deliberately written in DD/MM/YYYY -- the format Indian accounting packages export -- so the
    round trip exercises batch date-format detection rather than the trivial ISO path.
    """
    session = SessionLocal()
    try:
        rows = session.execute(
            text(
                """
                SELECT i.invoice_number, c.name, i.amount_paise, i.outstanding_paise,
                       i.issue_date, i.due_date, c.gstin
                FROM invoices i
                JOIN counterparties c ON c.id = i.counterparty_id
                WHERE i.merchant_id = (SELECT id FROM merchants ORDER BY created_at LIMIT 1)
                ORDER BY i.invoice_number
                """
            )
        ).all()
        distinct_names = session.execute(
            text(
                """
                SELECT count(DISTINCT c.id)
                FROM invoices i JOIN counterparties c ON c.id = i.counterparty_id
                WHERE i.merchant_id = (SELECT id FROM merchants ORDER BY created_at LIMIT 1)
                """
            )
        ).scalar_one()
    finally:
        session.close()

    if not rows:
        pytest.skip("database is not seeded; run `python -m app.seed`")

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        ["Bill No", "Party Name", "Bill Amount", "Balance Due", "Bill Date", "Due Dt", "GSTIN"]
    )
    total_outstanding = 0
    for number, party, amount, outstanding, issue, due, gstin in rows:
        total_outstanding += outstanding
        writer.writerow(
            [
                number,
                party,
                f"{amount / 100:.2f}",
                f"{outstanding / 100:.2f}",
                issue.strftime("%d/%m/%Y"),
                due.strftime("%d/%m/%Y"),
                gstin or "",
            ]
        )
    return buffer.getvalue().encode("utf-8"), len(rows), distinct_names


def test_seed_export_reupload_produces_the_correct_counts(
    client: TestClient, api_merchant: uuid.UUID
) -> None:
    """Export -> upload -> counts match. Every header used here is a Tally/Busy-style variant
    (``Bill No``, ``Party Name``, ``Bill Amount``, ``Due Dt``), so the rule-based mapper is doing
    real work rather than matching our own canonical names."""
    data, invoice_count, counterparty_count = _export_seed_to_csv()

    response = client.post(
        f"{API}/batches",
        headers=_auth(api_merchant),
        files={"file": ("seed_export.csv", data, "text/csv")},
        data={"name": "seed round trip"},
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["created"] == invoice_count, "every seeded invoice should import cleanly"
    assert body["repair_queue"] == 0, "the seed's own data must contain no defects"
    assert body["duplicates"] == 0
    assert body["counterparties_quarantined"] == counterparty_count

    # The Tally-style headers all resolved by rule -- no LLM, no merchant intervention.
    mapping = body["column_mapping"]
    assert mapping["Bill No"] == "invoice_number"
    assert mapping["Party Name"] == "counterparty_name"
    assert mapping["Bill Amount"] == "amount"
    assert mapping["Balance Due"] == "outstanding"
    assert mapping["Bill Date"] == "issue_date"
    assert mapping["Due Dt"] == "due_date"
    assert body["unmapped_headers"] == []


def test_reuploading_the_export_twice_creates_nothing_new(
    client: TestClient, api_merchant: uuid.UUID
) -> None:
    data, invoice_count, counterparty_count = _export_seed_to_csv()
    files = {"file": ("seed_export.csv", data, "text/csv")}

    first = client.post(f"{API}/batches", headers=_auth(api_merchant), files=files)
    assert first.status_code == 201
    assert first.json()["created"] == invoice_count

    second = client.post(
        f"{API}/batches",
        headers=_auth(api_merchant),
        files={"file": ("seed_export.csv", data, "text/csv")},
    )
    assert second.status_code == 201
    body = second.json()
    assert body["created"] == 0
    assert body["duplicates"] == invoice_count
    # Distinct counterparties, not rows: 34 parties across 120 invoices.
    assert body["counterparties_matched"] == counterparty_count
    assert body["counterparties_quarantined"] == 0, "no new counterparties on a re-upload"


def test_aging_after_reupload_matches_the_seed(client: TestClient, api_merchant: uuid.UUID) -> None:
    """days_past_due must survive the CSV round trip -- proof the dates were read correctly.

    If DD/MM were misread as MM/DD, these numbers would diverge wildly.
    """
    data, _, _ = _export_seed_to_csv()
    client.post(
        f"{API}/batches",
        headers=_auth(api_merchant),
        files={"file": ("seed_export.csv", data, "text/csv")},
    )

    session = SessionLocal()
    try:
        mismatches = session.execute(
            text(
                """
                SELECT count(*)
                FROM invoices mine
                JOIN invoices seeded
                  ON seeded.invoice_number = mine.invoice_number
                 AND seeded.merchant_id = (SELECT id FROM merchants ORDER BY created_at LIMIT 1)
                WHERE mine.merchant_id = :m
                  AND mine.due_date IS DISTINCT FROM seeded.due_date
                """
            ),
            {"m": api_merchant},
        ).scalar_one()
        assert mismatches == 0, "due dates changed across the CSV round trip"

        buckets = session.execute(
            text(
                "SELECT aging_bucket, count(*) FROM invoices WHERE merchant_id = :m "
                "GROUP BY 1 ORDER BY 1"
            ),
            {"m": api_merchant},
        ).all()
    finally:
        session.close()

    assert buckets, "aging must be computed at ingest, not left null"
    assert all(bucket is not None for bucket, _count in buckets)


def test_the_messy_fixture_lands_in_the_repair_queue(
    client: TestClient, api_merchant: uuid.UUID
) -> None:
    """The seed's deliberately defective upload, ingested through the real endpoint.

    The fixture carries 10 data rows: 2 clean context rows (one using the deliberate name variant
    ``Sundaram Auto Comp.``) and 8 authored as defective. Seven of those eight are caught here.

    The eighth, ``INV-2026-9005`` (``03/04/2026`` / ``03/05/2026``), is defective only *in
    isolation*: read on its own the date is ambiguous. Batch-level format detection -- which
    agents/backend.md mandates -- resolves it, because other rows in the same file carry days > 12
    and prove the file is DD/MM. Flagging it anyway would mean ignoring the batch evidence, which
    is precisely the behaviour the spec forbids. See the note in the Phase 1 summary.

    ``INV-2026-9010`` is caught on its GSTIN (``BADGSTIN123``) rather than its amount or date:
    ``88,000.50`` parses exactly to 8800050 paise, and ``2026/03/01`` is unambiguous ISO. The
    row is still a repair, which is what the fixture intends.
    """
    if not MESSY_FIXTURE.exists():
        pytest.skip("messy_upload.csv fixture not present; run `python -m app.seed`")

    data = MESSY_FIXTURE.read_bytes()
    response = client.post(
        f"{API}/batches",
        headers=_auth(api_merchant),
        files={"file": ("messy_upload.csv", data, "text/csv")},
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["repair_queue"] == 7
    assert body["created"] == 3  # 2 context rows + INV-2026-9005

    repairs = client.get(
        f"{API}/batches/{body['batch_id']}/repairs", headers=_auth(api_merchant)
    ).json()
    assert repairs["total"] == 7

    codes = {item["raw"]["Invoice #"]: item["error_code"] for item in repairs["items"]}
    assert codes == {
        "INV-2026-9003": "missing_due_date",
        "INV-2026-9004": "unparseable_amount",
        "INV-2026-9006": "missing_counterparty",
        "INV-2026-9007": "non_positive_amount",
        "INV-2026-9008": "impossible_date",
        "INV-2026-9009": "due_before_issue",
        "INV-2026-9010": "invalid_gstin",
    }
    # Every repair row carries a human-readable reason, not just a code.
    assert all(item["error_detail"] for item in repairs["items"])


def test_the_messy_fixture_name_variant_fuzzy_matches(
    client: TestClient, api_merchant: uuid.UUID
) -> None:
    """``Sundaram Auto Comp.`` must attach to the full name, not create a second record."""
    if not MESSY_FIXTURE.exists():
        pytest.skip("messy_upload.csv fixture not present")

    # Seed the full-form name first, the way a real merchant's earlier upload would have.
    seed_csv = (
        b"Invoice #,Customer,Amount,Invoice Date,Due Date\n"
        b"INV-PRE-001,Sundaram Auto Components Pvt Ltd,100000,18/02/2026,20/03/2026\n"
    )
    client.post(
        f"{API}/batches",
        headers=_auth(api_merchant),
        files={"file": ("pre.csv", seed_csv, "text/csv")},
    )
    client.post(
        f"{API}/batches",
        headers=_auth(api_merchant),
        files={"file": ("messy_upload.csv", MESSY_FIXTURE.read_bytes(), "text/csv")},
    )

    session = SessionLocal()
    try:
        names = [
            row[0]
            for row in session.execute(
                text("SELECT name FROM counterparties WHERE merchant_id = :m"),
                {"m": api_merchant},
            ).all()
        ]
    finally:
        session.close()

    assert names.count("Sundaram Auto Components Pvt Ltd") == 1
    assert "Sundaram Auto Comp." not in names, "the abbreviation must merge, not duplicate"


def _rewrite_export(data: bytes, mutate) -> bytes:
    """Re-emit an exported CSV with `mutate(row)` applied to each data row."""
    reader = csv.reader(io.StringIO(data.decode()))
    header = next(reader)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    for row in reader:
        writer.writerow(mutate(row))
    return out.getvalue().encode()


def _fetch_state(merchant_id: uuid.UUID, numbers: list[str]) -> dict[str, tuple]:
    session = SessionLocal()
    try:
        return {
            row[0]: (row[1], row[2], row[3], row[4], row[5])
            for row in session.execute(
                text(
                    "SELECT invoice_number, recovery_state, touch_count, current_tone_tier, "
                    "inferred_cause, outstanding_paise FROM invoices "
                    "WHERE merchant_id = :m AND invoice_number = ANY(:nums)"
                ),
                {"m": merchant_id, "nums": numbers},
            ).all()
        }
    finally:
        session.close()


def test_reingesting_the_seed_export_does_not_reset_in_flight_recovery(
    client: TestClient, api_merchant: uuid.UUID
) -> None:
    """The rule with the worst blast radius, exercised against real seed-scale data.

    The other round-trip tests upload into an empty tenant, so ``created=120, duplicates=0``
    proves parsing and matching but never exercises dedupe against *existing* rows -- and never
    touches this rule at all.

    Here the first upload establishes the book, ten invoices are advanced to a mid-escalation
    state, and the same file is re-uploaded with the money moved. A customer at tier 2 with 3
    touches must not drop back to a tier-1 nudge because the merchant re-exported their ledger.
    """
    data, invoice_count, _ = _export_seed_to_csv()

    first = client.post(
        f"{API}/batches",
        headers=_auth(api_merchant),
        files={"file": ("seed_export.csv", data, "text/csv")},
    )
    assert first.status_code == 201
    assert first.json()["created"] == invoice_count

    session = SessionLocal()
    try:
        in_flight = [
            row[0]
            for row in session.execute(
                text(
                    "SELECT invoice_number FROM invoices WHERE merchant_id = :m "
                    "ORDER BY invoice_number LIMIT 10"
                ),
                {"m": api_merchant},
            ).all()
        ]
        session.execute(
            text(
                "UPDATE invoices SET recovery_state = 'chasing', touch_count = 3, "
                "current_tone_tier = 2, inferred_cause = 'cash_crunch' "
                "WHERE merchant_id = :m AND invoice_number = ANY(:nums)"
            ),
            {"m": api_merchant, "nums": in_flight},
        )
        session.commit()
    finally:
        session.close()

    before = _fetch_state(api_merchant, in_flight)
    assert len(before) == 10
    assert all(row[0] == "chasing" for row in before.values())

    # Money has moved since the last export; recovery state has not.
    def halve_balance(row: list[str]) -> list[str]:
        row[3] = f"{float(row[3]) / 2:.2f}"  # Balance Due
        return row

    second = client.post(
        f"{API}/batches",
        headers=_auth(api_merchant),
        files={"file": ("seed_export_v2.csv", _rewrite_export(data, halve_balance), "text/csv")},
    )
    assert second.status_code == 201
    body = second.json()
    assert body["created"] == 0, "re-ingest must not create duplicates"
    assert body["duplicates"] == invoice_count
    assert body["updated"] == invoice_count

    after = _fetch_state(api_merchant, in_flight)
    for number, (state, touches, tier, cause, outstanding) in after.items():
        prior = before[number]
        assert state == prior[0] == "chasing", f"{number}: recovery_state was reset"
        assert touches == prior[1] == 3, f"{number}: touch_count was reset"
        assert tier == prior[2] == 2, f"{number}: current_tone_tier was reset"
        assert cause == prior[3], f"{number}: inferred_cause was reset"
        # ...and the one field that should have moved, did.
        assert outstanding == prior[4] // 2, f"{number}: outstanding_paise did not update"


def test_reingest_marks_an_invoice_paid_when_the_balance_reaches_zero(
    client: TestClient, api_merchant: uuid.UUID
) -> None:
    """The other half of the duplicate rule: payment_status follows the money."""
    data, _, _ = _export_seed_to_csv()
    client.post(
        f"{API}/batches",
        headers=_auth(api_merchant),
        files={"file": ("seed_export.csv", data, "text/csv")},
    )

    settled: list[str] = []

    def settle_first(row: list[str]) -> list[str]:
        if not settled:
            settled.append(row[0])
            row[3] = "0.00"
        return row

    client.post(
        f"{API}/batches",
        headers=_auth(api_merchant),
        files={"file": ("v2.csv", _rewrite_export(data, settle_first), "text/csv")},
    )

    session = SessionLocal()
    try:
        status, outstanding = session.execute(
            text(
                "SELECT payment_status, outstanding_paise FROM invoices "
                "WHERE merchant_id = :m AND invoice_number = :n"
            ),
            {"m": api_merchant, "n": settled[0]},
        ).one()
    finally:
        session.close()
    assert outstanding == 0
    assert status == "paid"


def test_an_entirely_ambiguous_file_sends_every_row_to_repair(
    client: TestClient, api_merchant: uuid.UUID
) -> None:
    """backend.md: when DD/MM cannot be told from MM/DD, the WHOLE batch goes to repair.

    messy_upload.csv cannot test this -- it contains days > 12 that resolve the format. This
    fixture has every part <= 12, so nothing in the file decides it. Read one way ``05/03/2026``
    is 5 March; read the other it is 3 May, a two-month swing in days_past_due.
    """
    if not AMBIGUOUS_FIXTURE.exists():
        pytest.skip("ambiguous_dates.csv fixture not present; run `python -m app.seed`")

    data = AMBIGUOUS_FIXTURE.read_bytes()
    row_count = len(data.decode().strip().splitlines()) - 1

    response = client.post(
        f"{API}/batches",
        headers=_auth(api_merchant),
        files={"file": ("ambiguous_dates.csv", data, "text/csv")},
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["created"] == 0, "not one row may be imported under a guessed format"
    assert body["repair_queue"] == row_count
    assert body["status"] == "awaiting_repair"

    repairs = client.get(
        f"{API}/batches/{body['batch_id']}/repairs", headers=_auth(api_merchant)
    ).json()
    assert repairs["total"] == row_count
    assert {item["error_code"] for item in repairs["items"]} == {"ambiguous_date_format"}
    detail = repairs["items"][0]["error_detail"]
    assert "DD/MM" in detail and "MM/DD" in detail

    session = SessionLocal()
    try:
        count = session.execute(
            text("SELECT count(*) FROM invoices WHERE merchant_id = :m"), {"m": api_merchant}
        ).scalar_one()
    finally:
        session.close()
    assert count == 0, "an unresolved batch must leave no invoices behind"


def test_iso_dates_rescue_the_otherwise_ambiguous_file(
    client: TestClient, api_merchant: uuid.UUID
) -> None:
    """The merchant way out: re-export the same dates as YYYY-MM-DD and every row imports."""
    if not AMBIGUOUS_FIXTURE.exists():
        pytest.skip("ambiguous_dates.csv fixture not present")

    def to_iso(row: list[str]) -> list[str]:
        for index in (3, 4):
            day, month, year = row[index].split("/")
            row[index] = f"{year}-{month}-{day}"
        return row

    response = client.post(
        f"{API}/batches",
        headers=_auth(api_merchant),
        files={
            "file": ("iso.csv", _rewrite_export(AMBIGUOUS_FIXTURE.read_bytes(), to_iso), "text/csv")
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["repair_queue"] == 0
    assert body["created"] == 6


def test_counterparty_counts_are_distinct_counterparties_not_rows(
    client: TestClient, api_merchant: uuid.UUID
) -> None:
    """api-contracts.md counts counterparties. A 34-counterparty file cannot match 86 of them.

    Counting per row made these track the invoice count instead (34 + 86 = 120 invoices).
    """
    data, invoice_count, counterparty_count = _export_seed_to_csv()
    assert counterparty_count < invoice_count, "the seed must have fewer parties than invoices"

    first = client.post(
        f"{API}/batches",
        headers=_auth(api_merchant),
        files={"file": ("seed_export.csv", data, "text/csv")},
    ).json()
    # Everything is new, so nothing is "matched" -- and neither figure may exceed the number of
    # distinct counterparties in the file.
    assert first["counterparties_matched"] == 0
    assert first["counterparties_quarantined"] == counterparty_count
    assert first["counterparties_matched"] <= counterparty_count

    second = client.post(
        f"{API}/batches",
        headers=_auth(api_merchant),
        files={"file": ("seed_export.csv", data, "text/csv")},
    ).json()
    # Now every counterparty already exists: all matched, none created, none newly quarantined.
    assert second["counterparties_matched"] == counterparty_count
    assert second["counterparties_quarantined"] == 0


def test_an_ambiguous_counterparty_row_goes_to_repair_not_a_coin_flip(
    client: TestClient, api_merchant: uuid.UUID
) -> None:
    """Two equally good name matches must never be resolved automatically."""
    established = (
        b"Invoice #,Customer,Amount,Invoice Date,Due Date\n"
        b"INV-AMB-001,Ram Industries,100000,18/02/2026,20/03/2026\n"
        b"INV-AMB-002,Ram Indane,150000,18/02/2026,20/03/2026\n"
    )
    client.post(
        f"{API}/batches",
        headers=_auth(api_merchant),
        files={"file": ("established.csv", established, "text/csv")},
    )

    abbreviated = (
        b"Invoice #,Customer,Amount,Invoice Date,Due Date\n"
        b"INV-AMB-003,Ram Ind.,200000,18/02/2026,20/03/2026\n"
    )
    body = client.post(
        f"{API}/batches",
        headers=_auth(api_merchant),
        files={"file": ("abbrev.csv", abbreviated, "text/csv")},
    ).json()

    assert body["created"] == 0
    assert body["repair_queue"] == 1

    repairs = client.get(
        f"{API}/batches/{body['batch_id']}/repairs", headers=_auth(api_merchant)
    ).json()
    item = repairs["items"][0]
    assert item["error_code"] == "ambiguous_counterparty"
    assert "Ram Industries" in item["error_detail"]
    assert "Ram Indane" in item["error_detail"]

    # No third counterparty was invented for a row that never became an invoice.
    session = SessionLocal()
    try:
        names = {
            row[0]
            for row in session.execute(
                text("SELECT name FROM counterparties WHERE merchant_id = :m"),
                {"m": api_merchant},
            ).all()
        }
    finally:
        session.close()
    assert names == {"Ram Industries", "Ram Indane"}
