"""Aging and exposure (FR-3). Pure SQL set operations -- no scoring model, no LLM, no ranking.

This module answers "how late is everything, and who owes us the most". Collectability scoring
and worklist ranking are Phase 2 and deliberately live elsewhere (``scoring/features.py``,
``scoring/model.py``), so the nightly aging refresh stays a cheap, deterministic UPDATE.

Everything is a single statement against the set, not a Python loop over rows: 120 invoices today
and 50,000 later both take one round trip, and the whole refresh is one transaction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.clock import today

# The MSME Act (Micro, Small and Medium Enterprises Development Act 2006, s.15) caps payment to a
# registered micro/small supplier at 45 days. Past that the buyer owes compound interest, which is
# real leverage in a dunning message -- so it is flagged separately (FR-3.3), never folded into a
# generic "very overdue" bucket.
MSME_THRESHOLD_DAYS = 45

# Buckets match the seed's _bucket_of and the aging distribution in agents/data-and-seed.md.
# Boundaries are inclusive-upper: dpd 30 is "0-30", dpd 31 is "31-60".
AGING_BUCKETS: tuple[str, ...] = ("current", "0-30", "31-60", "61-90", "90+")

# Aging is only meaningful for money still owed. A paid invoice's days_past_due is frozen at
# whatever it was when it settled -- letting it keep climbing would corrupt every historical
# average, and a settled invoice is not "getting later".
_OPEN_STATUSES = "('unpaid', 'partially_paid')"

_REFRESH_SQL = text(
    f"""
    UPDATE invoices AS i
    SET days_past_due = (CAST(:as_of AS date) - i.due_date),
        aging_bucket = CASE
            WHEN (CAST(:as_of AS date) - i.due_date) <= 0  THEN 'current'
            WHEN (CAST(:as_of AS date) - i.due_date) <= 30 THEN '0-30'
            WHEN (CAST(:as_of AS date) - i.due_date) <= 60 THEN '31-60'
            WHEN (CAST(:as_of AS date) - i.due_date) <= 90 THEN '61-90'
            ELSE '90+'
        END,
        crosses_msme_45 = (
            c.is_msme AND (CAST(:as_of AS date) - i.due_date) > :msme_days
        ),
        updated_at = now()
    FROM counterparties AS c
    WHERE c.id = i.counterparty_id
      AND i.merchant_id = :merchant_id
      AND i.payment_status IN {_OPEN_STATUSES}
      -- Idempotence: skip rows already correct, so a double-run is a no-op that touches
      -- nothing and leaves updated_at alone.
      AND (
          i.days_past_due IS DISTINCT FROM (CAST(:as_of AS date) - i.due_date)
          OR i.aging_bucket IS DISTINCT FROM CASE
              WHEN (CAST(:as_of AS date) - i.due_date) <= 0  THEN 'current'
              WHEN (CAST(:as_of AS date) - i.due_date) <= 30 THEN '0-30'
              WHEN (CAST(:as_of AS date) - i.due_date) <= 60 THEN '31-60'
              WHEN (CAST(:as_of AS date) - i.due_date) <= 90 THEN '61-90'
              ELSE '90+'
          END
          OR i.crosses_msme_45 IS DISTINCT FROM (
              c.is_msme AND (CAST(:as_of AS date) - i.due_date) > :msme_days
          )
      )
    """
)


def refresh_aging(db: Session, merchant_id: uuid.UUID, *, as_of: date | None = None) -> int:
    """Recompute ``days_past_due``, ``aging_bucket`` and ``crosses_msme_45`` for open invoices.

    Returns the number of rows actually changed. **Idempotent**: rows already holding the correct
    values are excluded by the WHERE clause, so running twice in one day updates nothing the
    second time and ``updated_at`` does not churn.

    ``as_of`` defaults to the current IST business date -- aging must roll over at midnight in
    Kolkata, not at 18:30 UTC the previous day.
    """
    result = cast(
        "CursorResult[Any]",
        db.execute(
            _REFRESH_SQL,
            {
                "merchant_id": merchant_id,
                "as_of": as_of or today(),
                "msme_days": MSME_THRESHOLD_DAYS,
            },
        ),
    )
    # Session.execute is typed as Result; an UPDATE always yields a CursorResult, which is the
    # only kind that carries rowcount.
    return result.rowcount or 0


@dataclass(frozen=True)
class CounterpartyExposure:
    """Open exposure to one counterparty and its share of the merchant's total book (FR-3.2)."""

    counterparty_id: uuid.UUID
    name: str
    is_msme: bool
    open_invoice_count: int
    outstanding_paise: int
    overdue_paise: int
    max_days_past_due: int
    msme_breach_count: int
    # 0.0-1.0. The concentration measure: what fraction of everything owed sits with this one
    # customer. A single counterparty above ~0.25 is a genuine solvency risk to the merchant.
    concentration: float


_EXPOSURE_SQL = text(
    f"""
    WITH open_invoices AS (
        SELECT i.counterparty_id,
               i.outstanding_paise,
               i.days_past_due,
               i.crosses_msme_45
        FROM invoices AS i
        WHERE i.merchant_id = :merchant_id
          AND i.payment_status IN {_OPEN_STATUSES}
    ),
    per_cp AS (
        SELECT o.counterparty_id,
               count(*)                                          AS open_invoice_count,
               sum(o.outstanding_paise)                           AS outstanding_paise,
               coalesce(sum(o.outstanding_paise)
                        FILTER (WHERE o.days_past_due > 0), 0)    AS overdue_paise,
               coalesce(max(o.days_past_due), 0)                  AS max_days_past_due,
               count(*) FILTER (WHERE o.crosses_msme_45)          AS msme_breach_count
        FROM open_invoices AS o
        GROUP BY o.counterparty_id
    )
    SELECT p.counterparty_id,
           c.name,
           c.is_msme,
           p.open_invoice_count,
           p.outstanding_paise,
           p.overdue_paise,
           p.max_days_past_due,
           p.msme_breach_count,
           p.outstanding_paise::float
               / NULLIF(sum(p.outstanding_paise) OVER (), 0)      AS concentration
    FROM per_cp AS p
    JOIN counterparties AS c ON c.id = p.counterparty_id
    ORDER BY p.outstanding_paise DESC
    """
)


def counterparty_exposure(db: Session, merchant_id: uuid.UUID) -> list[CounterpartyExposure]:
    """Open exposure and concentration per counterparty, largest first (FR-3.2).

    ``concentration`` is computed with a window function over the same result set, so the shares
    sum to 1.0 without a second query or a Python pass.
    """
    rows = db.execute(_EXPOSURE_SQL, {"merchant_id": merchant_id}).mappings().all()
    return [
        CounterpartyExposure(
            counterparty_id=row["counterparty_id"],
            name=row["name"],
            is_msme=row["is_msme"],
            open_invoice_count=row["open_invoice_count"],
            outstanding_paise=int(row["outstanding_paise"]),
            overdue_paise=int(row["overdue_paise"]),
            max_days_past_due=int(row["max_days_past_due"]),
            msme_breach_count=int(row["msme_breach_count"]),
            concentration=float(row["concentration"] or 0.0),
        )
        for row in rows
    ]


def bucket_of(days_past_due: int) -> str:
    """Python mirror of the SQL CASE, for callers holding a dpd but not a database row.

    Kept beside the SQL deliberately: if one changes and the other does not,
    ``test_aging_bucket_sql_matches_python`` fails.
    """
    if days_past_due <= 0:
        return "current"
    if days_past_due <= 30:
        return "0-30"
    if days_past_due <= 60:
        return "31-60"
    if days_past_due <= 90:
        return "61-90"
    return "90+"
