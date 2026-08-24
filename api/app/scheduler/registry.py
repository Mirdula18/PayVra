"""APScheduler setup and the /health snapshot.

BackgroundScheduler runs in-process alongside FastAPI, with job state persisted in Postgres via
``SQLAlchemyJobStore`` so per-invoice follow-ups survive a container restart (ADR-007).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler

from app.clock import IST, now_utc
from app.config import settings
from app.db import engine
from app.scheduler.state import SchedulerState

HEARTBEAT_JOB_ID = "heartbeat"
REFRESH_AGING_JOB_ID = "refresh_aging"
RESCORE_WORKLIST_JOB_ID = "rescore_worklist"


def build_scheduler() -> BackgroundScheduler:
    """Construct a scheduler backed by the Postgres job store."""
    jobstores = {"default": SQLAlchemyJobStore(engine=engine)}
    return BackgroundScheduler(
        jobstores=jobstores,
        timezone=IST,
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 60},
    )


def register_jobs(scheduler: BackgroundScheduler) -> None:
    """Register Phase 1 jobs. Idempotent: ``replace_existing`` avoids duplicates on restart."""
    scheduler.add_job(
        "app.scheduler.jobs:heartbeat",
        trigger="interval",
        seconds=settings.scheduler_heartbeat_seconds,
        id=HEARTBEAT_JOB_ID,
        replace_existing=True,
        # Fire once at startup so liveness is observable immediately, not one interval later.
        next_run_time=now_utc(),
    )

    # FR-3.1: aging refreshed nightly. 00:30 IST -- after the business day has rolled over in
    # Kolkata (app.clock.today), and before the morning dispatch window opens at 08:00, so the
    # first send of the day reads today's days_past_due and not yesterday's.
    scheduler.add_job(
        "app.scheduler.jobs:refresh_aging_all",
        trigger="cron",
        hour=0,
        minute=30,
        id=REFRESH_AGING_JOB_ID,
        replace_existing=True,
    )

    # FR-4.4: rescore nightly, folding in the previous day's engagement signals. 01:00 IST, a
    # clear 30 minutes after refresh_aging -- days_past_due is a scoring input, so scoring must
    # not race the job that computes it. Still hours before the 08:00 dispatch window opens.
    scheduler.add_job(
        "app.scheduler.jobs:rescore_worklist_all",
        trigger="cron",
        hour=1,
        minute=0,
        id=RESCORE_WORKLIST_JOB_ID,
        replace_existing=True,
    )


def health_snapshot(scheduler: BackgroundScheduler) -> dict[str, Any]:
    """Scheduler status for ``GET /health``.

    Distinguishes *started* (``running``) from *actually executing* (``last_heartbeat_at``), and
    reports how many jobs are registered and when the next one fires.
    """
    jobs = scheduler.get_jobs()
    next_runs = [job.next_run_time for job in jobs if job.next_run_time is not None]
    next_dispatch: datetime | None = min(next_runs) if next_runs else None
    return {
        "running": scheduler.running,
        "jobs_registered": len(jobs),
        "last_heartbeat_at": SchedulerState.last_heartbeat_at,
        "next_dispatch": next_dispatch,
    }
