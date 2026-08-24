"""Prove TRUNCATE cannot empty audit_log.

Migration 0001 made audit_log append-only with two RULEs. RULEs do not cover TRUNCATE, so
`TRUNCATE audit_log CASCADE` silently emptied the table until migration 0002 added a BEFORE
TRUNCATE trigger. This asserts the hole stays closed, against a live database -- the same standard
as test_audit_append_only, because a compliance claim that is only true in the migration text is
not a compliance claim.

Both cases run inside a transaction that is rolled back, so the database is left untouched.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.audit.log import record
from app.db import SessionLocal
from app.enums import ActorType
from app.models.merchant import Merchant


def _seed_entry(db) -> uuid.UUID:
    merchant = Merchant(id=uuid.uuid4(), name="TruncateTest", email="truncate@test.local")
    db.add(merchant)
    db.flush()
    record(
        db,
        merchant_id=merchant.id,
        actor=ActorType.SYSTEM,
        action_type="test.truncate",
        subject_type="invoice",
        subject_id=uuid.uuid4(),
        outcome="executed",
        rationale="truncate guard proof",
    )
    db.flush()
    return merchant.id


@pytest.mark.usefixtures("db_available")
def test_truncate_audit_log_is_rejected() -> None:
    """A direct TRUNCATE must raise, not silently succeed."""
    db = SessionLocal()
    try:
        _seed_entry(db)
        before = db.execute(text("SELECT count(*) FROM audit_log")).scalar_one()
        assert before > 0

        with pytest.raises(DBAPIError) as excinfo:
            db.execute(text("TRUNCATE audit_log"))
        assert "append-only" in str(excinfo.value)
    finally:
        db.rollback()
        db.close()


@pytest.mark.usefixtures("db_available")
def test_truncate_cascade_from_merchants_is_rejected() -> None:
    """The indirect path matters more: TRUNCATE merchants CASCADE reaches audit_log via its FK."""
    db = SessionLocal()
    try:
        _seed_entry(db)

        with pytest.raises(DBAPIError) as excinfo:
            db.execute(text("TRUNCATE merchants CASCADE"))
        assert "append-only" in str(excinfo.value)
    finally:
        db.rollback()
        db.close()


@pytest.mark.usefixtures("db_available")
def test_audit_log_rows_survive_a_rejected_truncate() -> None:
    """After the rejected TRUNCATE is rolled back, the entries are still there."""
    db = SessionLocal()
    try:
        _seed_entry(db)
        before = db.execute(text("SELECT count(*) FROM audit_log")).scalar_one()

        savepoint = db.begin_nested()
        try:
            db.execute(text("TRUNCATE audit_log"))
        except DBAPIError:
            savepoint.rollback()

        after = db.execute(text("SELECT count(*) FROM audit_log")).scalar_one()
        assert after == before
    finally:
        db.rollback()
        db.close()
