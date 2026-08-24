"""Scheduled job callables.

Phase 0 ships only the heartbeat. Later phases add ``refresh_aging``, ``rescore_worklist``,
``plan_day``, ``dispatch_window``, ``promise_sweep``, ``link_hygiene``, and ``digest`` as plain
callables taking ``merchant_id`` (ADR-007 keeps them Celery-portable). Do not add framework
decorators here.
"""

from __future__ import annotations

from app.clock import now_utc
from app.scheduler.state import SchedulerState


def heartbeat() -> None:
    """Liveness beat.

    Writing ``last_heartbeat_at`` every interval is what makes a *wedged* scheduler visible:
    ``scheduler.running`` can be ``True`` while the loop never fires, so "started" and "actually
    executing" must be distinguishable on ``GET /health`` (ADR-007 calls a silent dead scheduler a
    failure mode). This job does no business work and sends nothing.
    """
    SchedulerState.last_heartbeat_at = now_utc()
