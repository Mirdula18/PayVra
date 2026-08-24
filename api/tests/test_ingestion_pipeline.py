"""End-to-end ingestion against a live database.

Covers the two rules with the worst blast radius if they regress:

* re-ingesting a batch must not reset an in-flight recovery sequence;
* an ambiguous-date file must send the **whole batch** to repair, not import half of it.

Every test runs inside a transaction that is rolled back, so the seeded database is untouched.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import BatchRowStatus, BatchStatus, PaymentStatus, RecoveryState, RepairErrorCode
from app.ingestion import pipeline
from app.ingestion.parsers import parse_csv
from app.models.batch import Batch
from app.models.batch_row import BatchRow
from app.models.counterparty import Counterparty
from app.models.invoice import Invoice
from app.models.merchant import Merchant

pytestmark = pytest.mark.usefixtures("db_available")


CLEAN_CSV = b"""Invoice #,Customer,Amount,Invoice Date,Due Date,GSTIN
INV-T-001,Sundaram Auto Components Pvt Ltd,245000,05/03/2026,04/04/2026,
INV-T-002,Krishna Textiles,88000,12/02/2026,13/03/2026,
INV-T-003,Meridian Logistics LLP,410000,18/01/2026,17/02/2026,
"""

# Every date part <= 12: nothing in this file proves DD/MM or MM/DD.
AMBIGUOUS_CSV = b"""Invoice #,Customer,Amount,Invoice Date,Due Date
INV-A-001,Krishna Textiles,100000,03/04/2026,03/05/2026
INV-A-002,Anand Enterprises,200000,01/02/2026,01/03/2026
INV-A-003,Highland Ceramics,300000,05/06/2026,05/07/2026
"""


@pytest.fixture()
def db(db_session: Session) -> Session:
    return db_session


@pytest.fixture()
def merchant(db: Session) -> Merchant:
    row = Merchant(id=uuid.uuid4(), name="IngestTest", email=f"{uuid.uuid4()}@test.local")
    db.add(row)
    db.flush()
    return row


def _new_batch(db: Session, merchant: Merchant, filename: str = "upload.csv") -> Batch:
    batch = Batch(
        id=uuid.uuid4(),
        merchant_id=merchant.id,
        filename=filename,
        status=BatchStatus.PARSING.value,
    )
    db.add(batch)
    db.flush()
    return batch


def _ingest(db: Session, merchant: Merchant, data: bytes) -> pipeline.IngestOutcome:
    from app.ingestion import mapper

    parsed = parse_csv(data)
    batch = _new_batch(db, merchant)
    mapping = mapper.map_headers(parsed.headers).mapping
    return pipeline.ingest_rows(db, batch=batch, parsed=parsed, mapping=mapping)


# --- happy path -------------------------------------------------------------------------------


def test_clean_file_creates_invoices_and_counterparties(db: Session, merchant: Merchant) -> None:
    outcome = _ingest(db, merchant, CLEAN_CSV)
    assert outcome.created == 3
    assert outcome.repairs == 0
    assert outcome.counterparties_created == 3
    # FR-2.3: a brand-new counterparty has no consent basis, so it is quarantined on arrival.
    assert outcome.counterparties_quarantined == 3
    assert outcome.total_outstanding_paise == (245000 + 88000 + 410000) * 100


def test_aging_is_computed_at_ingest_not_left_at_zero(db: Session, merchant: Merchant) -> None:
    _ingest(db, merchant, CLEAN_CSV)
    invoices = list(db.execute(select(Invoice).where(Invoice.merchant_id == merchant.id)).scalars())
    assert invoices
    assert all(inv.aging_bucket is not None for inv in invoices)
    # Dates are in the past relative to the seeded 2026-08 anchor, so these are overdue.
    assert all(inv.days_past_due > 0 for inv in invoices)


# --- the duplicate rule -----------------------------------------------------------------------


def test_reingest_updates_money_but_never_resets_recovery_state(
    db: Session, merchant: Merchant
) -> None:
    """The rule that protects an in-flight dunning sequence.

    A customer mid-escalation at tier 3 must not drop back to tier 1 and start getting polite
    nudges again because the merchant re-uploaded their ledger.
    """
    _ingest(db, merchant, CLEAN_CSV)
    invoice = db.execute(
        select(Invoice).where(
            Invoice.merchant_id == merchant.id, Invoice.invoice_number == "INV-T-001"
        )
    ).scalar_one()

    # Simulate an in-flight recovery.
    invoice.recovery_state = RecoveryState.ESCALATED.value
    invoice.touch_count = 4
    invoice.current_tone_tier = 3
    db.flush()
    invoice_id = invoice.id

    # Same invoice number, less outstanding: a partial payment landed since the last export.
    updated_csv = CLEAN_CSV.replace(
        b"INV-T-001,Sundaram Auto Components Pvt Ltd,245000,05/03/2026,04/04/2026,",
        b"INV-T-001,Sundaram Auto Components Pvt Ltd,245000,05/03/2026,04/04/2026,\n"
        b"INV-T-004,Krishna Textiles,50000,01/03/2026,31/03/2026,",
    )
    outcome = _ingest(db, merchant, updated_csv)

    db.refresh(invoice)
    assert invoice.id == invoice_id, "must update in place, not create a second row"
    assert invoice.recovery_state == RecoveryState.ESCALATED.value
    assert invoice.touch_count == 4
    assert invoice.current_tone_tier == 3
    assert outcome.duplicates == 3
    assert outcome.created == 1  # only the genuinely new INV-T-004


def test_reingest_applies_a_partial_payment(db: Session, merchant: Merchant) -> None:
    _ingest(db, merchant, CLEAN_CSV)
    partial = CLEAN_CSV.replace(
        b"INV-T-002,Krishna Textiles,88000,12/02/2026,13/03/2026,",
        b"INV-T-002,Krishna Textiles,88000,12/02/2026,13/03/2026,",
    )
    # Add an outstanding column carrying the residual.
    partial = partial.replace(b",GSTIN\n", b",GSTIN,Balance Due\n")
    partial = partial.replace(
        b"88000,12/02/2026,13/03/2026,\n", b"88000,12/02/2026,13/03/2026,,30000\n"
    )
    partial = partial.replace(
        b"245000,05/03/2026,04/04/2026,\n", b"245000,05/03/2026,04/04/2026,,245000\n"
    )
    partial = partial.replace(
        b"410000,18/01/2026,17/02/2026,\n", b"410000,18/01/2026,17/02/2026,,410000\n"
    )

    _ingest(db, merchant, partial)
    invoice = db.execute(
        select(Invoice).where(
            Invoice.merchant_id == merchant.id, Invoice.invoice_number == "INV-T-002"
        )
    ).scalar_one()
    assert invoice.outstanding_paise == 30000 * 100
    assert invoice.payment_status == PaymentStatus.PARTIALLY_PAID.value


def test_duplicate_counterparty_name_variant_merges_not_duplicates(
    db: Session, merchant: Merchant
) -> None:
    _ingest(db, merchant, CLEAN_CSV)
    variant = b"""Invoice #,Customer,Amount,Invoice Date,Due Date
INV-T-900,Sundaram Auto Comp.,120000,18/03/2026,17/04/2026
"""
    outcome = _ingest(db, merchant, variant)
    assert outcome.counterparties_created == 0
    assert outcome.counterparties_matched == 1
    names = [
        c.name
        for c in db.execute(
            select(Counterparty).where(Counterparty.merchant_id == merchant.id)
        ).scalars()
    ]
    assert names.count("Sundaram Auto Components Pvt Ltd") == 1
    assert "Sundaram Auto Comp." not in names


# --- ambiguous dates fail the whole batch ------------------------------------------------------


def test_ambiguous_dates_send_the_whole_batch_to_repair(db: Session, merchant: Merchant) -> None:
    """Not one row. All of them. A half-imported file under a guessed format is the worst case."""
    outcome = _ingest(db, merchant, AMBIGUOUS_CSV)
    assert outcome.created == 0
    assert outcome.repairs == 3

    rows = list(db.execute(select(BatchRow)).scalars())
    ambiguous = [r for r in rows if r.error_code == RepairErrorCode.AMBIGUOUS_DATE_FORMAT.value]
    assert len(ambiguous) == 3
    assert all(r.status == BatchRowStatus.REPAIR_NEEDED.value for r in ambiguous)
    # No invoice may exist from an unresolved batch.
    assert not list(db.execute(select(Invoice).where(Invoice.merchant_id == merchant.id)).scalars())


def test_ambiguous_batch_error_names_the_offending_values(db: Session, merchant: Merchant) -> None:
    _ingest(db, merchant, AMBIGUOUS_CSV)
    row = db.execute(select(BatchRow)).scalars().first()
    assert row is not None and row.error_detail is not None
    assert "DD/MM" in row.error_detail and "MM/DD" in row.error_detail


def test_one_unambiguous_row_rescues_the_whole_batch(db: Session, merchant: Merchant) -> None:
    """Adding a single day > 12 resolves the format for every other row in the file."""
    rescued = AMBIGUOUS_CSV + b"INV-A-004,Krishna Textiles,400000,18/06/2026,18/07/2026\n"
    outcome = _ingest(db, merchant, rescued)
    assert outcome.repairs == 0
    assert outcome.created == 4


# --- repair queue -----------------------------------------------------------------------------


def test_defective_rows_land_in_the_repair_queue_with_specific_codes(
    db: Session, merchant: Merchant
) -> None:
    messy = b"""Invoice #,Customer,Amount,Invoice Date,Due Date,GSTIN
INV-M-001,Krishna Textiles,245000,18/02/2026,20/03/2026,
INV-M-002,Deccan Steel Traders Pvt Ltd,156000,18/02/2026,,
INV-M-003,Anand Enterprises,Rs. Twelve Thousand,01/03/2026,31/03/2026,
INV-M-004,,72000,20/01/2026,19/02/2026,
INV-M-005,Gujarat Polymers Pvt Ltd,-5000,10/02/2026,12/03/2026,
INV-M-006,Surya Pipes & Fittings,199000,32/02/2026,03/03/2026,
INV-M-007,Sri Lakshmi Agencies,134500,15/03/2026,14/02/2026,
INV-M-008,Highland Ceramics,88000,15/03/2026,14/04/2026,BADGSTIN123
"""
    outcome = _ingest(db, merchant, messy)
    assert outcome.created == 1
    assert outcome.repairs == 7

    rows = list(db.execute(select(BatchRow)).scalars())
    codes = {r.row_number: r.error_code for r in rows if r.error_code}
    assert codes[3] == RepairErrorCode.MISSING_DUE_DATE.value
    assert codes[4] == RepairErrorCode.UNPARSEABLE_AMOUNT.value
    assert codes[5] == RepairErrorCode.MISSING_COUNTERPARTY.value
    assert codes[6] == RepairErrorCode.NON_POSITIVE_AMOUNT.value
    assert codes[7] == RepairErrorCode.IMPOSSIBLE_DATE.value
    assert codes[8] == RepairErrorCode.DUE_BEFORE_ISSUE.value
    assert codes[9] == RepairErrorCode.INVALID_GSTIN.value


def test_a_repaired_row_becomes_an_invoice(db: Session, merchant: Merchant) -> None:
    from app.ingestion import mapper

    messy = b"""Invoice #,Customer,Amount,Invoice Date,Due Date
INV-R-001,Krishna Textiles,245000,18/02/2026,
INV-R-002,Anand Enterprises,150000,18/02/2026,20/03/2026
"""
    parsed = parse_csv(messy)
    batch = _new_batch(db, merchant)
    mapping = mapper.map_headers(parsed.headers).mapping
    pipeline.ingest_rows(db, batch=batch, parsed=parsed, mapping=mapping)

    broken = db.execute(
        select(BatchRow).where(
            BatchRow.batch_id == batch.id,
            BatchRow.status == BatchRowStatus.REPAIR_NEEDED.value,
        )
    ).scalar_one()

    row, error = pipeline.reprocess_row(
        db, batch=batch, batch_row=broken, corrected={"Due Date": "2026-03-20"}
    )
    assert error is None
    assert row.status == BatchRowStatus.REPAIRED.value
    assert row.invoice_id is not None
    assert batch.repair_count == 0
    assert batch.status == BatchStatus.COMPLETE.value


def test_a_still_broken_repair_is_rejected_not_accepted(db: Session, merchant: Merchant) -> None:
    from app.ingestion import mapper

    messy = b"""Invoice #,Customer,Amount,Invoice Date,Due Date
INV-R-003,Krishna Textiles,245000,18/02/2026,
"""
    parsed = parse_csv(messy)
    batch = _new_batch(db, merchant)
    mapping = mapper.map_headers(parsed.headers).mapping
    pipeline.ingest_rows(db, batch=batch, parsed=parsed, mapping=mapping)
    broken = db.execute(select(BatchRow).where(BatchRow.batch_id == batch.id)).scalar_one()

    row, error = pipeline.reprocess_row(
        db, batch=batch, batch_row=broken, corrected={"Due Date": "not a date"}
    )
    assert error is not None
    assert row.status == BatchRowStatus.REPAIR_NEEDED.value
    assert row.invoice_id is None
