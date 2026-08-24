"""Scoring engine: feature directions, determinism, and the reason string (ADR-008, FR-4).

No database. The model is pure arithmetic over a feature vector, which is the property that makes
it testable at all -- and the property ADR-008 chose it for.
"""

from __future__ import annotations

import uuid

import pytest

from app.scoring import features as feat
from app.scoring import model
from app.scoring.worklist import ClausePolarity, build_reason, reason_clauses, score_invoice


def make_features(**overrides: object) -> feat.InvoiceFeatures:
    """A deliberately neutral invoice: every normalised feature at 0.0 unless overridden.

    Neutral-at-zero means a single override is the *only* thing moving the logit, which is what
    makes the per-feature direction tests isolating rather than merely suggestive.
    """
    base: dict[str, object] = {
        "invoice_id": uuid.UUID(int=1),
        "payment_reliability": 0.0,
        "broken_promise_count": 0.0,
        "engagement_rate": 0.0,
        "has_dispute": 0.0,
        "days_past_due": 0.0,
        "lifetime_revenue": 0.0,
        "touch_count": 0.0,
        "amount_at_risk": 0.0,
        "exposure_share": 0.0,
        "raw_outstanding_paise": 10_000_000,  # ₹1L
        "raw_terms_days": 30,
    }
    base.update(overrides)
    return feat.InvoiceFeatures(**base)  # type: ignore[arg-type]


# --- ADR-008 weights are what the ADR says -------------------------------------------------------


def test_weights_match_adr_008_verbatim() -> None:
    """If ADR-008 is retuned, this test is the reminder to update the ADR *and* the code."""
    assert model.W_PAYMENT_RELIABILITY == 0.30
    assert model.W_BROKEN_PROMISE_COUNT == -0.25
    assert model.W_ENGAGEMENT_RATE == 0.20
    assert model.W_HAS_DISPUTE == -0.40
    assert model.W_DAYS_PAST_DUE == -0.15
    assert model.W_LIFETIME_REVENUE == 0.10
    assert model.W_TOUCH_COUNT == -0.10


def test_urgency_tiers_match_adr_008() -> None:
    assert model.URGENCY_BASE == 1.0
    assert model.URGENCY_MSME_45 == 1.3
    assert model.URGENCY_OPEN_PROMISE == 0.5


# --- each feature moves the score in the ADR-008 direction, in isolation --------------------------


@pytest.mark.parametrize(
    ("feature", "direction"),
    [
        ("payment_reliability", +1),
        ("broken_promise_count", -1),
        ("engagement_rate", +1),
        ("has_dispute", -1),
        ("days_past_due", -1),
        ("lifetime_revenue", +1),
        ("touch_count", -1),
    ],
)
def test_each_weighted_feature_moves_the_score_its_stated_direction(
    feature: str, direction: int
) -> None:
    neutral = model.p_collectable(make_features())
    moved = model.p_collectable(make_features(**{feature: 1.0}))
    if direction > 0:
        assert moved > neutral, f"{feature} should raise collectability"
    else:
        assert moved < neutral, f"{feature} should lower collectability"


@pytest.mark.parametrize("feature", ["amount_at_risk", "exposure_share"])
def test_unweighted_features_do_not_move_the_score(feature: str) -> None:
    """FR-4.1 names eight inputs; ADR-008 weights seven.

    ``amount_at_risk`` is not missing -- FR-4.2 multiplies it in at the priority stage.
    ``exposure_share`` genuinely has no ADR-008 weight, and carries an explicit 0.0 rather than an
    invented one. This test pins that as a deliberate state, so giving it a weight is a visible
    change to both the ADR and here.
    """
    neutral = model.p_collectable(make_features())
    assert model.p_collectable(make_features(**{feature: 1.0})) == neutral


def test_dispute_is_the_strongest_single_negative() -> None:
    """ADR-008 calls disputes "not a collections problem" and weights them hardest."""
    drops = {
        name: model.p_collectable(make_features())
        - model.p_collectable(make_features(**{name: 1.0}))
        for name in ("broken_promise_count", "has_dispute", "days_past_due", "touch_count")
    }
    assert drops["has_dispute"] == max(drops.values())


# --- the dominance requirement --------------------------------------------------------------------


def test_disputed_invoice_ranks_below_an_identical_undisputed_one() -> None:
    """Equal value, equal age -- the only difference is the dispute."""
    clean = score_invoice(make_features(days_past_due=0.4, raw_days_past_due=72))
    disputed = score_invoice(
        make_features(days_past_due=0.4, raw_days_past_due=72, has_dispute=1.0)
    )
    assert disputed.priority < clean.priority
    assert disputed.p_collectable < clean.p_collectable


def test_the_dispute_is_named_in_the_reason() -> None:
    scored = score_invoice(make_features(has_dispute=1.0, raw_days_past_due=30))
    assert "disputed" in scored.reason


# --- urgency multiplier ---------------------------------------------------------------------------


def test_open_promise_suppresses_by_half() -> None:
    plain = score_invoice(make_features(raw_days_past_due=40))
    promised = score_invoice(make_features(raw_days_past_due=40, has_open_promise_not_yet_due=True))
    assert promised.urgency == model.URGENCY_OPEN_PROMISE
    assert promised.priority == pytest.approx(float(plain.priority) * 0.5, rel=1e-3)


def test_msme_uplift_raises_priority() -> None:
    plain = score_invoice(make_features(raw_days_past_due=50))
    msme = score_invoice(make_features(raw_days_past_due=50, crosses_msme_45=True))
    assert msme.urgency == model.URGENCY_MSME_45
    assert msme.priority > plain.priority


def test_an_open_promise_beats_the_msme_uplift() -> None:
    """Both true: we gave our word more recently than we acquired the leverage, so we wait."""
    features = make_features(crosses_msme_45=True, has_open_promise_not_yet_due=True)
    assert model.urgency_multiplier(features) == model.URGENCY_OPEN_PROMISE


def test_open_promise_row_says_to_hold_off() -> None:
    scored = score_invoice(make_features(raw_days_past_due=20, has_open_promise_not_yet_due=True))
    assert "hold off" in scored.reason


# --- determinism ------------------------------------------------------------------------------


def test_same_input_same_score_and_same_reason_every_run() -> None:
    """ADR-008 rejects LLM ranking precisely so this holds. Ranking is arithmetic."""
    features = make_features(
        payment_reliability=0.7,
        engagement_rate=0.4,
        days_past_due=0.33,
        broken_promise_count=0.33,
        lifetime_revenue=0.8,
        touch_count=0.5,
        raw_days_past_due=60,
        raw_broken_promises=1,
        raw_touch_count=3,
        raw_messages_sent=4,
        raw_engagement_events=3,
        raw_avg_days_to_pay=42.0,
        raw_lifetime_revenue_paise=250_000_000,
    )
    runs = [score_invoice(features) for _ in range(25)]
    assert len({r.p_collectable for r in runs}) == 1
    assert len({r.priority for r in runs}) == 1
    assert len({r.reason for r in runs}) == 1


def test_contribution_order_is_stable_under_ties() -> None:
    """Two equal-magnitude contributions must not swap between runs and flap the reason."""
    # touch_count (-0.10 x 1.0) and lifetime_revenue (+0.10 x 1.0) tie exactly on magnitude.
    features = make_features(touch_count=1.0, lifetime_revenue=1.0)
    orders = [[c.feature for c in model.contributions(features)] for _ in range(10)]
    assert all(order == orders[0] for order in orders)


# --- the reason string ------------------------------------------------------------------------


def test_reason_is_never_empty_even_with_no_signal() -> None:
    """FR-4.3: required on every row. A blank would force the UI to special-case it."""
    reason = build_reason(make_features(), urgency=1.0)
    assert reason
    assert reason.endswith(".")


def test_reason_leads_with_money_and_age() -> None:
    scored = score_invoice(make_features(raw_outstanding_paise=42_000_000, raw_days_past_due=68))
    assert scored.reason.startswith("₹4.2L, 68 days.")


def test_reason_handles_an_invoice_not_yet_due() -> None:
    scored = score_invoice(make_features(raw_outstanding_paise=12_000_000, raw_days_past_due=-5))
    assert "due in 5 days" in scored.reason


def test_reason_quotes_at_most_three_features() -> None:
    features = make_features(
        payment_reliability=1.0,
        engagement_rate=1.0,
        broken_promise_count=1.0,
        days_past_due=1.0,
        touch_count=1.0,
        lifetime_revenue=1.0,
        raw_broken_promises=3,
        raw_touch_count=5,
        raw_messages_sent=6,
        raw_engagement_events=9,
        raw_avg_days_to_pay=20.0,
        raw_lifetime_revenue_paise=500_000_000,
        raw_days_past_due=150,
    )
    body = build_reason(features, urgency=1.0).split(". ", 1)[1]
    assert body.count(";") <= 2  # three clauses, two separators


def test_reason_does_not_claim_contact_that_did_not_happen() -> None:
    """An invoice with touches but no recorded message must not say both."""
    scored = score_invoice(
        make_features(raw_touch_count=1, raw_messages_sent=0, raw_days_past_due=10)
    )
    assert "not been contacted" not in scored.reason


def test_reason_pluralises() -> None:
    scored = score_invoice(make_features(raw_touch_count=1, touch_count=0.2, raw_days_past_due=10))
    assert "1 times" not in scored.reason


# --- feature extraction -----------------------------------------------------------------------


def test_payment_reliability_is_measured_against_terms_not_absolute_days() -> None:
    """45 days is excellent on net-60 and poor on net-15."""
    assert feat.payment_reliability(45, 60) == 1.0
    assert feat.payment_reliability(45, 15) == 0.0


def test_unknown_payment_history_is_neutral_not_bad() -> None:
    """A new customer has not proved they are unreliable."""
    assert feat.payment_reliability(None, 30) == feat.NO_EVIDENCE


def test_never_contacted_is_neutral_not_zero_engagement() -> None:
    """Silence from us is not silence from them."""
    assert feat.engagement_rate(0, 0, 0, 0) == feat.NO_EVIDENCE
    assert feat.engagement_rate(4, 0, 0, 0) == 0.0


def test_engagement_saturates_at_one() -> None:
    assert feat.engagement_rate(2, 2, 2, 5) == 1.0


def test_not_yet_due_does_not_score_better_than_due_today() -> None:
    """Negative dpd floors at 0.0 -- an invoice that is not late is not *less* than not late."""
    context = feat.ScoringContext(
        merchant_id=uuid.uuid4(), as_of=__import__("datetime").date(2026, 8, 24)
    )
    row = feat.ScoringRow(
        invoice_id=uuid.uuid4(),
        counterparty_id=uuid.uuid4(),
        counterparty_name="Test",
        outstanding_paise=100,
        days_past_due=-30,
        terms_days=30,
        touch_count=0,
        inferred_cause="unknown",
        stop_reason=None,
        crosses_msme_45=False,
        avg_days_to_pay=None,
        broken_promise_count=0,
        lifetime_revenue_paise=0,
        counterparty_outstanding_paise=100,
        messages_sent=0,
        opens=0,
        clicks=0,
        replies=0,
        open_promise_not_yet_due=False,
    )
    assert feat.extract(row, context).days_past_due == 0.0


def test_dispute_detected_from_either_cause_or_stop_reason() -> None:
    assert feat.has_dispute("dispute", None) is True
    assert feat.has_dispute("unknown", "disputed") is True
    assert feat.has_dispute("cash_crunch", None) is False


def test_features_module_does_not_import_the_model() -> None:
    """ADR-008's migration path depends on extraction being independent of combination.

    If features.py ever imports model.py, swapping in LightGBM stops being a one-file change.
    Parsed rather than grepped, so a docstring that merely *mentions* the model does not trip it.
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(feat.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)

    offenders = {name for name in imported if "scoring.model" in name or "scoring.worklist" in name}
    assert not offenders, f"features.py must not import {offenders}"


# --- pre-due suppression (ADR-008 urgency) ---


def test_pre_due_invoice_is_suppressed() -> None:
    """A worklist ranks expected *incremental* recovery. An invoice that pays itself has none."""
    overdue = score_invoice(make_features(raw_days_past_due=10))
    pre_due = score_invoice(make_features(raw_days_past_due=-10))
    assert pre_due.urgency == model.URGENCY_PRE_DUE
    assert overdue.urgency == model.URGENCY_BASE
    assert pre_due.priority < overdue.priority


def test_pre_due_beats_the_msme_uplift() -> None:
    """Inside terms is not yet a statutory-interest problem; the clock has not started."""
    features = make_features(raw_days_past_due=-5, crosses_msme_45=True)
    assert model.urgency_multiplier(features) == model.URGENCY_PRE_DUE


def test_pre_due_beats_an_open_promise() -> None:
    """Both say wait; the more fundamental fact is that nothing is overdue to chase."""
    features = make_features(raw_days_past_due=-5, has_open_promise_not_yet_due=True)
    assert model.urgency_multiplier(features) == model.URGENCY_PRE_DUE


def test_due_today_is_not_pre_due() -> None:
    """dpd == 0 is due, not early. The boundary must not silently suppress today's work."""
    assert model.urgency_multiplier(make_features(raw_days_past_due=0)) == model.URGENCY_BASE


def test_pre_due_row_says_so() -> None:
    scored = score_invoice(make_features(raw_days_past_due=-3))
    # The composer capitalises the leading clause, so compare case-insensitively.
    assert "not due yet" in scored.reason.lower()


def test_pre_due_is_suppressed_but_never_filtered_out() -> None:
    """Suppression is a ranking decision. A pre-due row still scores and still gets a reason."""
    scored = score_invoice(make_features(raw_days_past_due=-20, payment_reliability=1.0))
    assert scored.priority > 0
    assert scored.reason


# --- a reason string must justify its rank, never argue against it ---


def test_a_negative_never_leads_the_reason() -> None:
    """The bug this rule exists for: a row ranked 10th of 116 reading "recovery odds are thin"."""
    features = make_features(
        days_past_due=1.0,
        touch_count=1.0,
        broken_promise_count=1.0,
        raw_days_past_due=128,
        raw_touch_count=5,
        raw_broken_promises=3,
        raw_outstanding_paise=94_000_000,
    )
    clauses = reason_clauses(features)
    assert clauses
    assert clauses[0].polarity is not ClausePolarity.NEGATIVE


def test_at_most_one_negative_clause() -> None:
    features = make_features(
        payment_reliability=1.0,
        days_past_due=1.0,
        touch_count=1.0,
        broken_promise_count=1.0,
        has_dispute=1.0,
        raw_days_past_due=140,
        raw_touch_count=6,
        raw_broken_promises=3,
        raw_avg_days_to_pay=20.0,
    )
    negatives = [c for c in reason_clauses(features) if c.polarity is ClausePolarity.NEGATIVE]
    assert len(negatives) <= 1


def test_a_negative_is_phrased_as_a_caveat_not_a_verdict() -> None:
    features = make_features(
        payment_reliability=1.0,
        days_past_due=1.0,
        raw_days_past_due=128,
        raw_avg_days_to_pay=25.0,
        raw_terms_days=30,
    )
    negatives = [c for c in reason_clauses(features) if c.polarity is ClausePolarity.NEGATIVE]
    assert negatives
    assert negatives[0].text.startswith("though ")
    assert "odds are thin" not in negatives[0].text


def test_an_all_negative_row_explains_why_it_still_ranks() -> None:
    """Do not lead with a caveat and do not list reasons against acting."""
    features = make_features(
        days_past_due=1.0,
        touch_count=1.0,
        broken_promise_count=1.0,
        raw_days_past_due=128,
        raw_touch_count=5,
        raw_broken_promises=3,
        raw_outstanding_paise=94_000_000,
    )
    reason = build_reason(features, urgency=1.0)
    assert "large enough to pursue" in reason
    assert "₹9.4L" in reason


def test_urgency_framing_leads_when_it_applies() -> None:
    features = make_features(crosses_msme_45=True, days_past_due=0.5, raw_days_past_due=90)
    clauses = reason_clauses(features)
    assert clauses[0].polarity is ClausePolarity.NEUTRAL
    assert "MSME" in clauses[0].text


def test_reason_clauses_mirror_the_urgency_precedence() -> None:
    """If the framing and the multiplier disagree, the merchant is told one thing and ranked by
    another."""
    both = make_features(raw_days_past_due=-5, crosses_msme_45=True)
    assert "not due yet" in reason_clauses(both)[0].text
    assert model.urgency_multiplier(both) == model.URGENCY_PRE_DUE
