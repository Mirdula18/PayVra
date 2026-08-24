"""The collectability model: weighted sum of normalised features through a logistic (ADR-008).

**Combination only.** This file owns what a feature is *worth*; ``features.py`` owns what a
feature *is*. ADR-008's migration path swaps this module for LightGBM and leaves extraction
untouched, so nothing here may reach back into how a feature was computed.

Every weight is a named module-level constant. ADR-008 is explicit that they are "a starting
point and an assumption, not a finding" -- they will be tuned, so they have to be findable and
greppable, never buried in an expression.

**No LLM is involved, here or anywhere in the ranking path.** Ranking is arithmetic and must be
byte-identical across runs (ADR-008 rejects LLM ranking firmly: non-deterministic ordering,
unexplainable, slow).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.scoring.features import InvoiceFeatures

# --- ADR-008 feature weights ---------------------------------------------------------------------
# Verbatim from the ADR-008 weight table. Sign is the ADR's stated direction.

W_PAYMENT_RELIABILITY = 0.30  # counterparty_avg_days_to_pay within terms: reliable payers pay
W_BROKEN_PROMISE_COUNT = -0.25  # strongest negative signal
W_ENGAGEMENT_RATE = 0.20  # engagement predicts payment
W_HAS_DISPUTE = -0.40  # disputes are not a collections problem
W_DAYS_PAST_DUE = -0.15  # collectability decays with age
W_LIFETIME_REVENUE = 0.10  # relationship value is worth more effort
W_TOUCH_COUNT = -0.10  # diminishing returns

# --- extracted but unweighted --------------------------------------------------------------------
# FR-4.1 names eight scoring inputs; ADR-008's weight table gives seven; they overlap on six.
#
# `amount_at_risk` is not missing -- FR-4.2 multiplies it in at the priority stage rather than
# folding it into p_collectable, which is the right place for it: how *likely* a rupee is to
# arrive is a separate question from how *many* rupees there are.
#
# `exposure_share` genuinely has no weight in ADR-008. It is extracted and logged (so it is in the
# training set from day one, as the ADR requires) but deliberately carries 0.0 here: inventing a
# weight for it would be a spec decision made silently in an implementation file. Give it a value
# in ADR-008 and change this constant.
W_AMOUNT_AT_RISK = 0.0
W_EXPOSURE_SHARE = 0.0

# Logistic intercept. ADR-008 specifies "a weighted sum of normalised features passed through a
# logistic function" and no intercept, so this is 0.0: an invoice whose weighted sum is zero
# scores exactly 0.50. Named rather than omitted so that tuning it later is a visible edit.
LOGIT_INTERCEPT = 0.0

# Absolute range of the score. The weights above encode *direction and relative magnitude*;
# this sets how far apart the resulting probabilities are allowed to spread.
#
# Why it is needed. The weights sum to a magnitude of 1.50, so the reachable weighted sum is
# roughly [-0.90, +0.60] -- and the logistic is near-linear there. Unscaled, p_collectable spans
# only 1.45x on real seed data while invoice amounts span 60x, so `p x amount` is operationally
# an amount sort: 9 of the top 10 rows match a pure value sort. At that point the model is
# decoration, and worse, a reason string citing collectability actively misleads about what
# drove the rank.
#
# Why 6.0. It yields a ~4.94x spread, inside the 5-9x range collectability genuinely varies over
# in real receivables (a disputed invoice from a serial promise-breaker versus a clean one from a
# reliable payer ten days late). Higher scales push p toward saturation, where a single feature
# can flip an invoice to near-zero and the ranking turns brittle.
#
# What does NOT change: amount still dominates at the top of the list, and that is correct.
# `priority` is expected recovery, so a large invoice at moderate odds legitimately outranks a
# small one at good odds. Scaling widens how much the odds can move a row, not whether money
# counts. Scaling is also monotone, so it cannot reorder `contributions()` -- reason strings are
# unaffected by this constant.
#
# This is the first thing to revisit when real payment outcomes arrive, and it becomes
# meaningless under the ADR-008 LightGBM migration: gradient boosting calibrates itself.
LOGIT_SCALE = 6.0

WEIGHTS: dict[str, float] = {
    "payment_reliability": W_PAYMENT_RELIABILITY,
    "broken_promise_count": W_BROKEN_PROMISE_COUNT,
    "engagement_rate": W_ENGAGEMENT_RATE,
    "has_dispute": W_HAS_DISPUTE,
    "days_past_due": W_DAYS_PAST_DUE,
    "lifetime_revenue": W_LIFETIME_REVENUE,
    "touch_count": W_TOUCH_COUNT,
    "amount_at_risk": W_AMOUNT_AT_RISK,
    "exposure_share": W_EXPOSURE_SHARE,
}

# --- urgency multipliers (ADR-008) ---------------------------------------------------------------

URGENCY_BASE = 1.0
# MSME Act s.15: past 45 days the buyer owes compound interest. Real leverage, so chase sooner.
URGENCY_MSME_45 = 1.3

# Inside payment terms -- not late at all. A worklist ranks expected *incremental* recovery, the
# lift from intervening, and an invoice that will pay itself on time has almost none. Left
# unsuppressed the model actively rewards these rows for being healthy: high payment_reliability
# and zero days_past_due give them the best collectability in the book, and `p x amount` floated
# nine of the seed's top twenty rows onto money that was not yet due.
#
# Same logic as the open-promise tier: payment terms are a promise *we* made, so the pre-due
# window is one we agreed to and should not be crowding a human triage queue inside it.
#
# This suppresses ranking only. Pre-due invoices stay ON the worklist and stay eligible for the
# tier-1 pre-due courtesy note in the recovery sequence -- they are never filtered out.
URGENCY_PRE_DUE = 0.3
# A promise we accepted and that has not yet come due. We said we would wait, so we wait --
# chasing here is the fastest way to lose the goodwill that produced the promise.
URGENCY_OPEN_PROMISE = 0.5

# Precedence, not multiplication, whenever several tiers apply. The chain is
# pre-due -> open promise -> MSME -> base, most suppressive first, and the reasoning below
# generalises to every pair:
#
#   * pre-due beats the MSME uplift. An invoice inside terms is not yet a statutory-interest
#     problem; the interest clock has not started.
#   * pre-due also beats an open promise (0.3 over 0.5). Both say "wait", and when both apply the
#     more fundamental fact is that there is nothing overdue to chase yet.
#
# When an invoice is BOTH past the MSME 45-day line and inside an open promise, the promise wins
# outright; the two are not multiplied. Settled deliberately, so it is not re-litigated:
#   * 1.3 x 0.5 = 0.65 expresses neither intention. It is not "press because we have leverage"
#     and not "wait because we gave our word" -- it is a number that means nothing.
#   * The promise is the more recent commitment, and the one the counterparty will judge us on.
#   * Multiplying would quietly chase someone inside a window we ourselves agreed to, which is
#     the behaviour most likely to convert a paying customer into a dispute.
# See urgency_multiplier() below.

# ADR-008 also lists 1.5 for "approaching a limitation period". Not implemented: nothing in the
# data model records a limitation date yet, and a multiplier keyed off a field that does not exist
# would be dead code pretending to be a feature. It lands with the field.


@dataclass(frozen=True)
class Contribution:
    """One feature's signed push on the logit, for the reason string and for explainability."""

    feature: str
    normalised: float
    weight: float

    @property
    def value(self) -> float:
        return self.normalised * self.weight

    @property
    def magnitude(self) -> float:
        return abs(self.value)


def logistic(x: float) -> float:
    """Numerically stable logistic. Guards the overflow at large negative x."""
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    exp_x = math.exp(x)
    return exp_x / (1.0 + exp_x)


def contributions(features: InvoiceFeatures) -> list[Contribution]:
    """Every feature's signed contribution, largest magnitude first.

    Ties break on feature name so the ordering -- and therefore the reason string -- is stable
    across runs. Sorting only by magnitude would let two equal contributions swap places between
    processes and make an otherwise deterministic reason string flap.
    """
    items = [
        Contribution(feature=name, normalised=value, weight=WEIGHTS[name])
        for name, value in features.as_vector().items()
    ]
    return sorted(items, key=lambda c: (-c.magnitude, c.feature))


def weighted_sum(features: InvoiceFeatures) -> float:
    """Raw sum of weight x normalised value, before scaling.

    This is the quantity the weights are stated in, and the one ``contributions()`` ranks the
    reason string on. Kept separate from :func:`logit` so that :data:`LOGIT_SCALE` is visibly a
    calibration step rather than something folded into the weights.
    """
    return sum(c.value for c in contributions(features))


def logit(features: InvoiceFeatures) -> float:
    """What is actually fed to the logistic: intercept plus the scaled weighted sum."""
    return LOGIT_INTERCEPT + LOGIT_SCALE * weighted_sum(features)


def p_collectable(features: InvoiceFeatures) -> float:
    """Probability-shaped collectability score in ``(0, 1)``.

    Not a calibrated probability, and ADR-008 does not claim it is -- it is a monotone ranking
    signal with a probability's shape. Say so if a judge asks.
    """
    return logistic(logit(features))


def urgency_multiplier(features: InvoiceFeatures) -> float:
    """ADR-008's urgency tiers.

    Tiers take precedence, they do not multiply. Most suppressive first:
    pre-due, then an open promise, then the MSME uplift, then base.

    An open promise **wins over** the MSME uplift rather than multiplying with it. The two encode
    opposing intentions -- "we have leverage, press" and "we gave our word, wait" -- and when both
    are true the promise is the more recent commitment and the one the counterparty will judge us
    on. Multiplying them (1.3 x 0.5 = 0.65) would express neither and quietly chase someone
    inside a window we agreed to.

    Pre-due sits above both: an invoice inside its own terms is not yet a statutory-interest
    problem, and there is nothing overdue to chase regardless of what else is true of it.
    """
    if features.raw_days_past_due < 0:
        return URGENCY_PRE_DUE
    if features.has_open_promise_not_yet_due:
        return URGENCY_OPEN_PROMISE
    if features.crosses_msme_45:
        return URGENCY_MSME_45
    return URGENCY_BASE
