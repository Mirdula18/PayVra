"""Single source of truth for the collection-period (DSO) figure.

One formula, defined once, used by the seed summary, the seeded ``metrics_snapshots`` rows, and
-- from Phase 1 -- ``GET /metrics``. It lives here rather than inline at each call site because
three independent implementations is exactly how the dashboard ends up contradicting the seed
mid-demo. If you need this number, import it; do not re-derive it.

The two metrics below are routinely confused and are NOT interchangeable:

* ``collection_period_days`` measures from the **issue** date and is amount-weighted. This is the
  ~73-day headline stat (docs/vision.md, agents/data-and-seed.md).
* ``mean_days_past_due`` measures from the **due** date and is unweighted. On the seeded batch it
  is ~27 days. It is not the headline stat and cannot be made to equal it -- see the note in
  agents/data-and-seed.md.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from decimal import ROUND_HALF_UP, Decimal


def quantize_days(value: Decimal) -> Decimal:
    """Round a day-count to 1 dp. Shared so every stored/reported figure has identical scale."""
    return value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def collection_period_days(
    invoices: Iterable[tuple[date, int]],
    *,
    as_of: date,
) -> Decimal:
    """Amount-weighted average age, in days, of the open receivables book.

    ``Σ (as_of − issue_date) × outstanding_paise ÷ Σ outstanding_paise``

    Callers pass ``(issue_date, outstanding_paise)`` pairs for invoices that are **not fully
    paid**; filtering is the caller's job, since the set differs by endpoint (whole book for the
    dashboard, a date window for ``GET /metrics``).

    Returns ``Decimal("0.0")`` for an empty book rather than raising, so a fresh merchant with no
    invoices renders as 0 instead of 500ing the dashboard.
    """
    numerator = Decimal(0)
    denominator = Decimal(0)
    for issue_date, outstanding_paise in invoices:
        weight = Decimal(outstanding_paise)
        numerator += Decimal((as_of - issue_date).days) * weight
        denominator += weight
    if denominator == 0:
        return Decimal("0.0")
    return quantize_days(numerator / denominator)


def mean_days_past_due(days_past_due: Iterable[int]) -> Decimal:
    """Unweighted mean of ``days_past_due``. Negative for invoices not yet due.

    Reported alongside :func:`collection_period_days` so the two are never mistaken for each
    other again. See agents/data-and-seed.md.
    """
    values = [Decimal(d) for d in days_past_due]
    if not values:
        return Decimal("0.0")
    return quantize_days(sum(values) / Decimal(len(values)))
