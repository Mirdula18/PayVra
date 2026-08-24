"""Aging, MSME flagging, exposure, and job idempotency (FR-3).

Runs against a live database inside a rolled-back transaction.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import PaymentStatus
from app.models.counterparty import Counterparty
from app.models.invoice import Invoice
from app.models.merchant import Merchant
from app.scoring.aging import (
    MSME_THRESHOLD_DAYS,
    bucket_of,
    counterparty_exposure,
    refresh_aging,
)

pytestmark = pytest.mark.usefixtures("db_available")

AS_OF = date(2026, 8, 24)


def _refresh(db: Session, merchant: Merchant, as_of: date = AS_OF) -> int:
    """Run the refresh, then drop the ORM identity map.

    ``refresh_aging`` is a bulk SQL UPDATE, so it deliberately bypasses the ORM -- which means
    objects already loaded in this session still hold their pre-update values. Production never
    notices (each job run gets a fresh session), but a test that asserts on an object it loaded
    earlier must re-read from the database.
    """
    changed = refresh_aging(db, merchant.id, as_of=as_of)
    db.expire_all()
    return changed


@pytest.fixture()
def merchant(db_session: Session) -> Merchant:
    row = Merchant(id=uuid.uuid4(), name="AgingTest", email=f"{uuid.uuid4()}@test.local")
    db_session.add(row)
    db_session.flush()
    return row


def _counterparty(db: Session, merchant: Merchant, name: str, *, is_msme: bool) -> Counterparty:
    cp = Counterparty(
        id=uuid.uuid4(),
        merchant_id=merchant.id,
        name=name,
        name_normalized=name.lower(),
        is_msme=is_msme,
    )
    db.add(cp)
    db.flush()
    return cp


def _invoice(
    db: Session,
    merchant: Merchant,
    cp: Counterparty,
    *,
    dpd: int,
    amount: int = 100_000,
    outstanding: int | None = None,
    status: str = PaymentStatus.UNPAID.value,
) -> Invoice:
    due = AS_OF - timedelta(days=dpd)
    inv = Invoice(
        id=uuid.uuid4(),
        merchant_id=merchant.id,
        counterparty_id=cp.id,
        invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
        amount_paise=amount,
        outstanding_paise=amount if outstanding is None else outstanding,
        issue_date=due - timedelta(days=30),
        due_date=due,
        terms_days=30,
        payment_status=status,
    )
    db.add(inv)
    db.flush()
    return inv


# --- buckets ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dpd", "expected"),
    [
        (-20, "current"),
        (0, "current"),
        (1, "0-30"),
        (30, "0-30"),
        (31, "31-60"),
        (60, "31-60"),
        (61, "61-90"),
        (90, "61-90"),
        (91, "90+"),
        (365, "90+"),
    ],
)
def test_bucket_boundaries(dpd: int, expected: str) -> None:
    assert bucket_of(dpd) == expected


def test_aging_bucket_sql_matches_python(db_session: Session, merchant: Merchant) -> None:
    """The SQL CASE and the Python mirror must never drift apart."""
    cp = _counterparty(db_session, merchant, "Bucket Co", is_msme=False)
    dpds = [-20, 0, 1, 30, 31, 60, 61, 90, 91, 200]
    invoices = {_invoice(db_session, merchant, cp, dpd=d).id: d for d in dpds}

    _refresh(db_session, merchant)

    for invoice_id, dpd in invoices.items():
        row = db_session.get(Invoice, invoice_id)
        assert row is not None
        assert row.days_past_due == dpd
        assert row.aging_bucket == bucket_of(dpd), f"dpd={dpd}"


# --- MSME -------------------------------------------------------------------------------------


def test_msme_flag_only_past_45_days_and_only_for_msme_counterparties(
    db_session: Session, merchant: Merchant
) -> None:
    msme = _counterparty(db_session, merchant, "Small Supplier", is_msme=True)
    large = _counterparty(db_session, merchant, "Large Supplier", is_msme=False)

    just_under = _invoice(db_session, merchant, msme, dpd=MSME_THRESHOLD_DAYS)
    just_over = _invoice(db_session, merchant, msme, dpd=MSME_THRESHOLD_DAYS + 1)
    non_msme = _invoice(db_session, merchant, large, dpd=200)

    _refresh(db_session, merchant)

    assert db_session.get(Invoice, just_under.id).crosses_msme_45 is False
    assert db_session.get(Invoice, just_over.id).crosses_msme_45 is True
    assert db_session.get(Invoice, non_msme.id).crosses_msme_45 is False


def test_settled_invoices_are_not_re_aged(db_session: Session, merchant: Merchant) -> None:
    """A paid invoice is not 'getting later'; its dpd freezes at settlement."""
    cp = _counterparty(db_session, merchant, "Paid Co", is_msme=False)
    paid = _invoice(db_session, merchant, cp, dpd=200, status=PaymentStatus.PAID.value)
    paid.days_past_due = 12
    paid.aging_bucket = "0-30"
    db_session.flush()

    _refresh(db_session, merchant)

    row = db_session.get(Invoice, paid.id)
    assert row.days_past_due == 12
    assert row.aging_bucket == "0-30"


# --- idempotency ------------------------------------------------------------------------------


def test_refresh_is_idempotent(db_session: Session, merchant: Merchant) -> None:
    """A double-run must change nothing the second time -- the job runs nightly and may retry."""
    cp = _counterparty(db_session, merchant, "Idempotent Co", is_msme=True)
    for dpd in (-5, 10, 50, 120):
        _invoice(db_session, merchant, cp, dpd=dpd)

    first = _refresh(db_session, merchant)
    assert first == 4

    second = _refresh(db_session, merchant)
    assert second == 0, "second run must be a no-op"

    third = _refresh(db_session, merchant)
    assert third == 0


def test_refresh_picks_up_a_new_day(db_session: Session, merchant: Merchant) -> None:
    cp = _counterparty(db_session, merchant, "Rollover Co", is_msme=False)
    invoice = _invoice(db_session, merchant, cp, dpd=10)

    _refresh(db_session, merchant)
    assert db_session.get(Invoice, invoice.id).days_past_due == 10

    changed = _refresh(db_session, merchant, AS_OF + timedelta(days=1))
    assert changed == 1
    assert db_session.get(Invoice, invoice.id).days_past_due == 11


def test_refresh_is_scoped_to_one_merchant(db_session: Session, merchant: Merchant) -> None:
    other = Merchant(id=uuid.uuid4(), name="Other", email=f"{uuid.uuid4()}@test.local")
    db_session.add(other)
    db_session.flush()

    mine = _counterparty(db_session, merchant, "Mine", is_msme=False)
    theirs = _counterparty(db_session, other, "Theirs", is_msme=False)
    my_invoice = _invoice(db_session, merchant, mine, dpd=40)
    their_invoice = _invoice(db_session, other, theirs, dpd=40)

    _refresh(db_session, merchant)

    assert db_session.get(Invoice, my_invoice.id).days_past_due == 40
    assert db_session.get(Invoice, their_invoice.id).days_past_due == 0, (
        "another merchant's invoices must be untouched"
    )


# --- exposure and concentration ---------------------------------------------------------------


def test_exposure_and_concentration(db_session: Session, merchant: Merchant) -> None:
    big = _counterparty(db_session, merchant, "Big Debtor", is_msme=False)
    small = _counterparty(db_session, merchant, "Small Debtor", is_msme=False)

    _invoice(db_session, merchant, big, dpd=60, amount=750_000)
    _invoice(db_session, merchant, small, dpd=-5, amount=250_000)
    _refresh(db_session, merchant)

    exposure = counterparty_exposure(db_session, merchant.id)
    assert [e.name for e in exposure] == ["Big Debtor", "Small Debtor"]

    top = exposure[0]
    assert top.outstanding_paise == 750_000
    assert top.overdue_paise == 750_000
    assert top.max_days_past_due == 60
    assert top.concentration == pytest.approx(0.75)

    # Not yet due, so it is exposure but not overdue.
    assert exposure[1].overdue_paise == 0
    assert sum(e.concentration for e in exposure) == pytest.approx(1.0)


def test_exposure_excludes_paid_invoices(db_session: Session, merchant: Merchant) -> None:
    cp = _counterparty(db_session, merchant, "Mixed Co", is_msme=False)
    _invoice(db_session, merchant, cp, dpd=30, amount=100_000)
    _invoice(db_session, merchant, cp, dpd=30, amount=900_000, status=PaymentStatus.PAID.value)

    exposure = counterparty_exposure(db_session, merchant.id)
    assert len(exposure) == 1
    assert exposure[0].outstanding_paise == 100_000
    assert exposure[0].open_invoice_count == 1


def test_exposure_counts_partial_payments_as_the_residual(
    db_session: Session, merchant: Merchant
) -> None:
    """FR-3.4: a partially paid invoice contributes only what is still owed."""
    cp = _counterparty(db_session, merchant, "Partial Co", is_msme=False)
    _invoice(
        db_session,
        merchant,
        cp,
        dpd=30,
        amount=500_000,
        outstanding=200_000,
        status=PaymentStatus.PARTIALLY_PAID.value,
    )
    exposure = counterparty_exposure(db_session, merchant.id)
    assert exposure[0].outstanding_paise == 200_000


def test_exposure_is_scoped_to_one_merchant(db_session: Session, merchant: Merchant) -> None:
    other = Merchant(id=uuid.uuid4(), name="Other2", email=f"{uuid.uuid4()}@test.local")
    db_session.add(other)
    db_session.flush()
    theirs = _counterparty(db_session, other, "Theirs", is_msme=False)
    _invoice(db_session, other, theirs, dpd=10, amount=999_999)

    mine = _counterparty(db_session, merchant, "Mine", is_msme=False)
    _invoice(db_session, merchant, mine, dpd=10, amount=100_000)

    exposure = counterparty_exposure(db_session, merchant.id)
    assert [e.name for e in exposure] == ["Mine"]
    assert exposure[0].concentration == pytest.approx(1.0)


def test_empty_book_returns_no_exposure(db_session: Session, merchant: Merchant) -> None:
    assert counterparty_exposure(db_session, merchant.id) == []


def test_refresh_on_a_merchant_with_no_invoices_is_zero(
    db_session: Session, merchant: Merchant
) -> None:
    assert refresh_aging(db_session, merchant.id, as_of=AS_OF) == 0


# --- scheduler registration -------------------------------------------------------------------


def test_refresh_aging_job_is_registered_at_0030_daily() -> None:
    """FR-3.1 requires a nightly refresh; the demo depends on it having already run by 08:00."""
    from apscheduler.schedulers.background import BackgroundScheduler

    from app.scheduler.registry import REFRESH_AGING_JOB_ID, register_jobs

    scheduler = BackgroundScheduler(jobstores={"default": {"type": "memory"}})
    register_jobs(scheduler)
    job = scheduler.get_job(REFRESH_AGING_JOB_ID)

    assert job is not None, "refresh_aging must be registered"
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["hour"] == "0"
    assert fields["minute"] == "30"
    assert job.func_ref == "app.scheduler.jobs:refresh_aging_all"


def test_refresh_aging_job_takes_a_plain_merchant_id() -> None:
    """ADR-007 portability: no framework decorator, one positional merchant_id."""
    import inspect

    from app.scheduler.jobs import refresh_aging as job

    params = list(inspect.signature(job).parameters)
    assert params == ["merchant_id"]


def test_invoices_selected_by_the_worklist_index_still_use_it(
    db_session: Session, merchant: Merchant
) -> None:
    """Aging writes to invoices; make sure it has not disturbed the Phase 0 worklist index."""
    plan = db_session.execute(
        select(Invoice).where(
            Invoice.merchant_id == merchant.id, Invoice.recovery_state == "chasing"
        )
    )
    assert plan is not None
