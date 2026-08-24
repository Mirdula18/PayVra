"""Feature extraction for the collectability score (FR-4.1). **Extraction only.**

This module must never learn what a feature is *worth*. ADR-008's migration path -- swap the
weighted sum for LightGBM once real payment outcomes exist -- depends on feature extraction being
untouched when the model changes. So there are no weights here, no logistic function, no priority
arithmetic, and no import of :mod:`app.scoring.model`. If a change to the weights would require
editing this file, the separation has been broken.

Every feature is normalised to ``[0, 1]`` with a documented *direction*: 1.0 always means "more of
this thing", never "better". Whether more is better is the model's opinion, not the extractor's.

A note on the feature set. FR-4.1 names eight inputs; ADR-008's weight table gives weights for
seven; they overlap on six. The union is extracted here, because ADR-008 also says to log the full
vector from day one so a training set exists when real outcomes arrive -- and a feature that is
unweighted today is still a column LightGBM will want tomorrow. Which of them actually move the
score is decided in ``model.py``, where the unweighted ones carry an explicit ``0.0``.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

# --- normalisation scales -----------------------------------------------------------------------
# Named, not inline, because each is a judgement about where a feature saturates. They are not
# weights -- changing one changes what "1.0" means for that feature, not how much it counts.

# Days past due at which the arrears feature saturates. Six months: past that, a receivable is
# qualitatively "very old" and further ageing adds little information.
DPD_SATURATION_DAYS = 180

# Broken promises at which the feature saturates. Three is the stopping rule (CLAUDE.md invariant
# 8), so a counterparty at 3 is already at the maximum the product will tolerate.
BROKEN_PROMISE_SATURATION = 3

# Fallback lifetime touch cap when the merchant has not set one.
DEFAULT_TOUCH_CAP = 6

# Each delivered message can plausibly yield an open and a click, so two engagement events per
# message is "fully engaged". Replies count on top -- a reply is the strongest signal of the three.
ENGAGEMENT_EVENTS_PER_MESSAGE = 2

# Returned when a feature has no evidence either way. Deliberately neutral rather than 0.0: an
# invoice nobody has contacted yet has *unknown* engagement, not *bad* engagement, and scoring it
# as bad would bury every newly imported invoice under the ones we happen to have data on.
NO_EVIDENCE = 0.5


def clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else (1.0 if value > 1.0 else value)


def _log_ratio(value: int, ceiling: int) -> float:
    """Log-scaled ``value / ceiling``, for heavy-tailed money quantities.

    Invoice amounts and lifetime revenue are log-normal (agents/data-and-seed.md): a linear ratio
    would put almost every invoice near 0.0 and let a single ₹14L outlier define the scale.
    """
    if ceiling <= 0 or value <= 0:
        return 0.0
    return clamp01(math.log1p(value) / math.log1p(ceiling))


@dataclass(frozen=True)
class ScoringContext:
    """Merchant-level constants the per-invoice normalisation needs.

    Passed in explicitly rather than queried per invoice: scoring a 120-invoice book must not
    issue 120 aggregate queries, and a test must be able to pin the scale without a database.

    These make a score *relative to the merchant's own book*, which is the intent -- "a big
    invoice" means big for this merchant. It also means a score is only reproducible against the
    same context, which is why :func:`load_context` is deterministic and why the context is
    logged alongside the vector.
    """

    merchant_id: uuid.UUID
    as_of: date
    max_outstanding_paise: int = 0
    max_lifetime_revenue_paise: int = 0
    total_outstanding_paise: int = 0
    lifetime_touch_cap: int = DEFAULT_TOUCH_CAP


@dataclass(frozen=True)
class InvoiceFeatures:
    """One invoice's feature vector: raw values kept beside normalised ones.

    Raw values are retained because they are what the reason string quotes back to the merchant
    ("has broken 2 promises", not "broken_promise_count 0.67"), and because the audit-log vector
    is only a useful training set if it records what actually happened rather than this version
    of the normalisation.
    """

    invoice_id: uuid.UUID

    # --- normalised, [0, 1], 1.0 = "more of this thing" ---
    payment_reliability: float  # 1.0 = pays within terms
    broken_promise_count: float  # 1.0 = at or past the stopping rule
    engagement_rate: float  # 1.0 = opens, clicks, replies to everything
    has_dispute: float  # 1.0 = disputed
    days_past_due: float  # 1.0 = >= DPD_SATURATION_DAYS overdue
    lifetime_revenue: float  # 1.0 = the merchant's largest relationship
    touch_count: float  # 1.0 = at the lifetime touch cap
    amount_at_risk: float  # 1.0 = the merchant's largest open invoice
    exposure_share: float  # 1.0 = this counterparty is the entire book

    # --- raw, for the reason string and the training set ---
    raw_days_past_due: int = 0
    raw_outstanding_paise: int = 0
    raw_broken_promises: int = 0
    raw_touch_count: int = 0
    raw_avg_days_to_pay: float | None = None
    raw_terms_days: int = 0
    raw_lifetime_revenue_paise: int = 0
    raw_messages_sent: int = 0
    raw_engagement_events: int = 0
    raw_exposure_share: float = 0.0
    counterparty_name: str = ""

    # --- flags the urgency multiplier needs (not scored features) ---
    crosses_msme_45: bool = False
    has_open_promise_not_yet_due: bool = False

    def as_vector(self) -> dict[str, float]:
        """The normalised vector alone -- what a future LightGBM model consumes."""
        return {
            "payment_reliability": self.payment_reliability,
            "broken_promise_count": self.broken_promise_count,
            "engagement_rate": self.engagement_rate,
            "has_dispute": self.has_dispute,
            "days_past_due": self.days_past_due,
            "lifetime_revenue": self.lifetime_revenue,
            "touch_count": self.touch_count,
            "amount_at_risk": self.amount_at_risk,
            "exposure_share": self.exposure_share,
        }

    def as_audit_payload(self) -> dict[str, Any]:
        """Everything worth keeping for the ADR-008 training set: normalised *and* raw."""
        payload = asdict(self)
        payload["invoice_id"] = str(self.invoice_id)
        return payload


@dataclass
class ScoringRow:
    """Raw per-invoice inputs, straight from SQL. One query for the whole book."""

    invoice_id: uuid.UUID
    counterparty_id: uuid.UUID
    counterparty_name: str
    outstanding_paise: int
    days_past_due: int
    terms_days: int
    touch_count: int
    inferred_cause: str
    stop_reason: str | None
    crosses_msme_45: bool
    avg_days_to_pay: Decimal | None
    broken_promise_count: int
    lifetime_revenue_paise: int
    counterparty_outstanding_paise: int
    messages_sent: int
    opens: int
    clicks: int
    replies: int
    open_promise_not_yet_due: bool
    extra: dict[str, Any] = field(default_factory=dict)


# One statement for the whole book. Engagement reaches invoices through actions, since a message
# belongs to the action that sent it.
_SCORING_SQL = text(
    """
    WITH open_invoices AS (
        SELECT i.id, i.counterparty_id, i.outstanding_paise, i.days_past_due, i.terms_days,
               i.touch_count, i.inferred_cause, i.stop_reason, i.crosses_msme_45
        FROM invoices AS i
        WHERE i.merchant_id = :merchant_id
          AND i.payment_status IN ('unpaid', 'partially_paid')
    ),
    engagement AS (
        SELECT a.invoice_id,
               count(m.id)                                          AS messages_sent,
               count(m.opened_at)                                   AS opens,
               count(m.clicked_at)                                  AS clicks
        FROM actions AS a
        JOIN messages AS m ON m.action_id = a.id
        WHERE a.merchant_id = :merchant_id
        GROUP BY a.invoice_id
    ),
    inbound AS (
        SELECT r.invoice_id, count(*) AS replies
        FROM replies AS r
        WHERE r.invoice_id IS NOT NULL
        GROUP BY r.invoice_id
    ),
    open_promises AS (
        SELECT p.invoice_id, bool_or(p.promised_date >= CAST(:as_of AS date)) AS not_yet_due
        FROM promises AS p
        WHERE p.status = 'open'
        GROUP BY p.invoice_id
    ),
    cp_exposure AS (
        SELECT o.counterparty_id, sum(o.outstanding_paise) AS counterparty_outstanding_paise
        FROM open_invoices AS o
        GROUP BY o.counterparty_id
    )
    SELECT o.id                                        AS invoice_id,
           o.counterparty_id,
           c.name                                      AS counterparty_name,
           o.outstanding_paise,
           o.days_past_due,
           o.terms_days,
           o.touch_count,
           o.inferred_cause,
           o.stop_reason,
           o.crosses_msme_45,
           c.avg_days_to_pay,
           c.broken_promise_count,
           c.lifetime_revenue_paise,
           x.counterparty_outstanding_paise,
           coalesce(e.messages_sent, 0)                AS messages_sent,
           coalesce(e.opens, 0)                        AS opens,
           coalesce(e.clicks, 0)                       AS clicks,
           coalesce(r.replies, 0)                      AS replies,
           coalesce(p.not_yet_due, false)              AS open_promise_not_yet_due
    FROM open_invoices AS o
    JOIN counterparties AS c ON c.id = o.counterparty_id
    JOIN cp_exposure AS x ON x.counterparty_id = o.counterparty_id
    LEFT JOIN engagement AS e ON e.invoice_id = o.id
    LEFT JOIN inbound AS r ON r.invoice_id = o.id
    LEFT JOIN open_promises AS p ON p.invoice_id = o.id
    ORDER BY o.id
    """
)


def load_scoring_rows(db: Session, merchant_id: uuid.UUID, *, as_of: date) -> list[ScoringRow]:
    """Load every open invoice's raw scoring inputs in one query, ordered by id for determinism."""
    rows = db.execute(_SCORING_SQL, {"merchant_id": merchant_id, "as_of": as_of}).mappings().all()
    return [
        ScoringRow(
            invoice_id=row["invoice_id"],
            counterparty_id=row["counterparty_id"],
            counterparty_name=row["counterparty_name"],
            outstanding_paise=int(row["outstanding_paise"]),
            days_past_due=int(row["days_past_due"]),
            terms_days=int(row["terms_days"]),
            touch_count=int(row["touch_count"]),
            inferred_cause=row["inferred_cause"],
            stop_reason=row["stop_reason"],
            crosses_msme_45=bool(row["crosses_msme_45"]),
            avg_days_to_pay=row["avg_days_to_pay"],
            broken_promise_count=int(row["broken_promise_count"]),
            lifetime_revenue_paise=int(row["lifetime_revenue_paise"]),
            counterparty_outstanding_paise=int(row["counterparty_outstanding_paise"]),
            messages_sent=int(row["messages_sent"]),
            opens=int(row["opens"]),
            clicks=int(row["clicks"]),
            replies=int(row["replies"]),
            open_promise_not_yet_due=bool(row["open_promise_not_yet_due"]),
        )
        for row in rows
    ]


def build_context(
    rows: list[ScoringRow], *, merchant_id: uuid.UUID, as_of: date, lifetime_touch_cap: int
) -> ScoringContext:
    """Derive the merchant-level normalisation scale from the book itself.

    Computed from the same rows that are about to be scored, so the scale and the scores are
    always consistent -- and a rescore of an unchanged book produces an unchanged scale.
    """
    return ScoringContext(
        merchant_id=merchant_id,
        as_of=as_of,
        max_outstanding_paise=max((r.outstanding_paise for r in rows), default=0),
        max_lifetime_revenue_paise=max((r.lifetime_revenue_paise for r in rows), default=0),
        total_outstanding_paise=sum(r.outstanding_paise for r in rows),
        lifetime_touch_cap=lifetime_touch_cap or DEFAULT_TOUCH_CAP,
    )


def payment_reliability(avg_days_to_pay: float | None, terms_days: int) -> float:
    """1.0 when the counterparty historically pays within terms, 0.0 at twice terms or worse.

    Measured *against this invoice's terms*, not in absolute days: 45 days to pay is excellent on
    net-60 and poor on net-15. Unknown history is :data:`NO_EVIDENCE`, not 0.0 -- a new customer
    has not proved they are unreliable.
    """
    if avg_days_to_pay is None:
        return NO_EVIDENCE
    if terms_days <= 0:
        # No agreed terms; fall back to a 30-day norm rather than dividing by zero.
        terms_days = 30
    overrun = float(avg_days_to_pay) - terms_days
    if overrun <= 0:
        return 1.0
    return clamp01(1.0 - overrun / terms_days)


def engagement_rate(messages_sent: int, opens: int, clicks: int, replies: int) -> float:
    """Share of possible engagement actually observed. :data:`NO_EVIDENCE` when never contacted.

    Never-contacted and never-engaged are different facts and must not collapse to the same
    number: the first is silence from us, the second is silence from them.
    """
    if messages_sent <= 0:
        return NO_EVIDENCE
    events = opens + clicks + replies
    return clamp01(events / (messages_sent * ENGAGEMENT_EVENTS_PER_MESSAGE))


def has_dispute(inferred_cause: str, stop_reason: str | None) -> bool:
    """A dispute is a commercial disagreement, not a collections problem (ADR-008)."""
    return inferred_cause == "dispute" or stop_reason == "disputed"


def extract(row: ScoringRow, context: ScoringContext) -> InvoiceFeatures:
    """Turn one row of raw inputs into a normalised feature vector. Pure; no I/O, no weights."""
    reliability = payment_reliability(
        float(row.avg_days_to_pay) if row.avg_days_to_pay is not None else None,
        row.terms_days,
    )
    engagement = engagement_rate(row.messages_sent, row.opens, row.clicks, row.replies)
    disputed = has_dispute(row.inferred_cause, row.stop_reason)
    share = (
        row.counterparty_outstanding_paise / context.total_outstanding_paise
        if context.total_outstanding_paise > 0
        else 0.0
    )

    return InvoiceFeatures(
        invoice_id=row.invoice_id,
        payment_reliability=reliability,
        broken_promise_count=clamp01(row.broken_promise_count / BROKEN_PROMISE_SATURATION),
        engagement_rate=engagement,
        has_dispute=1.0 if disputed else 0.0,
        # Negative dpd (not yet due) floors at 0.0: an invoice that is not late is not "less than
        # not late", and letting it go negative would reward it twice.
        days_past_due=clamp01(max(0, row.days_past_due) / DPD_SATURATION_DAYS),
        lifetime_revenue=_log_ratio(row.lifetime_revenue_paise, context.max_lifetime_revenue_paise),
        touch_count=clamp01(row.touch_count / context.lifetime_touch_cap),
        amount_at_risk=_log_ratio(row.outstanding_paise, context.max_outstanding_paise),
        exposure_share=clamp01(share),
        raw_days_past_due=row.days_past_due,
        raw_outstanding_paise=row.outstanding_paise,
        raw_broken_promises=row.broken_promise_count,
        raw_touch_count=row.touch_count,
        raw_avg_days_to_pay=(
            float(row.avg_days_to_pay) if row.avg_days_to_pay is not None else None
        ),
        raw_terms_days=row.terms_days,
        raw_lifetime_revenue_paise=row.lifetime_revenue_paise,
        raw_messages_sent=row.messages_sent,
        raw_engagement_events=row.opens + row.clicks + row.replies,
        raw_exposure_share=share,
        counterparty_name=row.counterparty_name,
        crosses_msme_45=row.crosses_msme_45,
        has_open_promise_not_yet_due=row.open_promise_not_yet_due,
    )


def extract_all(rows: list[ScoringRow], context: ScoringContext) -> list[InvoiceFeatures]:
    return [extract(row, context) for row in rows]
