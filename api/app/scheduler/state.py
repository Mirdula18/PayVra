"""Shared, in-process scheduler observability state.

Kept in its own module so both ``jobs`` (writer) and ``registry`` (reader) can import it without a
cycle. In-process is sufficient under ADR-007's single-process model: a fresh process legitimately
has not executed a beat yet, so ``last_heartbeat_at`` starting at ``None`` is correct, not a bug.
"""

from __future__ import annotations

from datetime import datetime


class SchedulerState:
    """Process-local scheduler liveness signal."""

    last_heartbeat_at: datetime | None = None
