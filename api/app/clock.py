"""Time helpers. Store UTC; convert to IST only for display and the ``time_window`` guardrail.

Never use naive datetimes. Never use ``datetime.utcnow()`` (it returns a naive object).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def now_utc() -> datetime:
    """Timezone-aware current time in UTC."""
    return datetime.now(UTC)


def today() -> date:
    """Current business date in IST.

    Aging, due-date maths, and seed anchoring all use the India business day, not UTC — at
    18:30 UTC it is already tomorrow in Kolkata.
    """
    return now_utc().astimezone(IST).date()
