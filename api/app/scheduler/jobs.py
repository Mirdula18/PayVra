"""Scheduled job callables.

Phase 2 ships the heartbeat, ``refresh_aging`` and ``rescore_worklist``. Later phases add
``plan_day``, ``dispatch_window``, ``promise_sweep``, ``link_hygiene``, and ``digest`` as plain
callables taking ``merchant_id`` (ADR-007 keeps them Celery-portable). Do not add framework
decorators here.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select

from app.clock import now_utc
from app.db import SessionLocal
from app.models.merchant import Merchant
from app.scheduler.state import SchedulerState

logger = logging.getLogger(__name__)


def heartbeat() -> None:
    """Liveness beat.

    Writing ``last_heartbeat_at`` every interval is what makes a *wedged* scheduler visible:
    ``scheduler.running`` can be ``True`` while the loop never fires, so "started" and "actually
    executing" must be distinguishable on ``GET /health`` (ADR-007 calls a silent dead scheduler a
    failure mode). This job does no business work and sends nothing.
    """
    SchedulerState.last_heartbeat_at = now_utc()


def refresh_aging(merchant_id: uuid.UUID) -> None:
    """Recompute days_past_due, aging_bucket and crosses_msme_45 for one merchant (FR-3.1).

    **Idempotent.** The underlying UPDATE skips rows already holding the correct values, so a
    double-run changes nothing and does not churn ``updated_at``. Nothing here sends anything;
    a repeat run cannot double-contact anyone.

    Takes ``merchant_id`` as a plain argument with no framework decorator, so the callable ports
    to Celery unchanged (ADR-007).
    """
    from app.scoring.aging import refresh_aging as refresh

    db = SessionLocal()
    try:
        changed = refresh(db, merchant_id)
        db.commit()
        logger.info("refresh_aging merchant=%s rows_changed=%d", merchant_id, changed)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def refresh_aging_all() -> None:
    """Fan out :func:`refresh_aging` across every merchant. This is what the 00:30 job runs.

    One session per merchant, and one merchant's failure does not abort the rest -- a single bad
    tenant must not leave every other tenant's aging stale for a day.
    """
    db = SessionLocal()
    try:
        merchant_ids = list(db.execute(select(Merchant.id)).scalars())
    finally:
        db.close()

    for merchant_id in merchant_ids:
        try:
            refresh_aging(merchant_id)
        except Exception:
            logger.exception("refresh_aging failed for merchant=%s", merchant_id)


def rescore_worklist(merchant_id: uuid.UUID) -> None:
    """Recompute collectability, priority and the reason string for one merchant (FR-4.4).

    **Idempotent.** An invoice whose score, priority and reason are all unchanged is skipped, so
    a second run in the same night updates nothing and writes no audit entry. That matters twice
    over: it keeps the job safe to retry, and it keeps the ADR-008 training set free of duplicate
    observations of a state that never changed.

    Runs after ``refresh_aging``, because days_past_due is a scoring input and scoring yesterday's
    ageing would be quietly wrong every morning.
    """
    from app.scoring.worklist import rescore

    db = SessionLocal()
    try:
        changed = rescore(db, merchant_id)
        db.commit()
        logger.info("rescore_worklist merchant=%s rows_changed=%d", merchant_id, changed)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def rescore_worklist_all() -> None:
    """Fan out :func:`rescore_worklist` across every merchant. This is what the 01:00 job runs."""
    db = SessionLocal()
    try:
        merchant_ids = list(db.execute(select(Merchant.id)).scalars())
    finally:
        db.close()

    for merchant_id in merchant_ids:
        try:
            rescore_worklist(merchant_id)
        except Exception:
            logger.exception("rescore_worklist failed for merchant=%s", merchant_id)
