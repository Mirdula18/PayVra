"""Counterparty matching: abbreviations must merge, similar-but-different names must not.

A false merge combines two companies' payment histories *and consent records*, which means
contacting someone who never consented. agents/backend.md: "a false merge is far worse than a
duplicate." These tests pin both sides of the 88 threshold.
"""

from __future__ import annotations

import uuid

import pytest

from app.ingestion.matcher import (
    MATCH_THRESHOLD,
    AmbiguousMatchError,
    MatchMethod,
    find_match,
    normalize_name,
    similarity,
)
from app.models.counterparty import Counterparty


def _cp(name: str, gstin: str | None = None) -> Counterparty:
    return Counterparty(
        id=uuid.uuid4(),
        merchant_id=uuid.uuid4(),
        name=name,
        name_normalized=normalize_name(name),
        gstin=gstin,
    )


# --- normalisation ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Sundaram Auto Components Pvt Ltd", "sundaram auto components"),
        ("Krishna Textiles", "krishna textiles"),
        ("Meridian Logistics LLP", "meridian logistics"),
        ("Surya Pipes & Fittings", "surya pipes fittings"),
        ("  Anand   Enterprises  ", "anand enterprises"),
        ("Zenith Marketing Private Limited", "zenith marketing"),
    ],
)
def test_normalize_name_strips_legal_suffixes_and_punctuation(raw: str, expected: str) -> None:
    assert normalize_name(raw) == expected


# --- the required merge -----------------------------------------------------------------------


def test_sundaram_abbreviation_merges() -> None:
    """The seed's deliberate name variant. Raw token_sort_ratio scores this 85.71 -- below
    threshold -- so abbreviation expansion is what makes it work."""
    score = similarity("Sundaram Auto Components Pvt Ltd", "Sundaram Auto Comp.")
    assert score >= MATCH_THRESHOLD, f"expected a merge, scored {score}"


def test_fr_1_4_sharma_example_merges() -> None:
    """FR-1.4 names this pair explicitly. Raw token_sort_ratio scores it only 71.43."""
    score = similarity("Sharma Enterprises Pvt Ltd", "Sharma Ent.")
    assert score >= MATCH_THRESHOLD, f"expected a merge, scored {score}"


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Krishna Textiles", "Krishna Textile"),
        ("Meridian Logistics LLP", "Meridian Logistics"),
        ("Konkan Seafoods Pvt Ltd", "Konkan Seafoods"),
        ("Bright Star Garments", "Bright Star Garment"),
    ],
)
def test_trivial_variants_merge(left: str, right: str) -> None:
    assert similarity(left, right) >= MATCH_THRESHOLD


# --- the required non-merge -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [
        # Both of these score ~85 -- close enough to be tempting, different enough to be a
        # different company. Highland Ceramics scores *exactly* the same 85.71 as the Sundaram
        # pair above, which is why the threshold alone cannot separate them.
        ("Highland Ceramics", "Highland Chemicals"),
        ("Royal Timber Traders", "Royal Timber Trading"),
        ("Anand Enterprises", "Anand Industries"),
        ("Bright Star Garments", "Bright Star Chemicals"),
        ("Sundaram Auto Components Pvt Ltd", "Sundaram Textiles Pvt Ltd"),
    ],
)
def test_genuinely_different_companies_do_not_merge(left: str, right: str) -> None:
    score = similarity(left, right)
    assert score < MATCH_THRESHOLD, f"false merge risk: {left!r} vs {right!r} scored {score}"


def test_abbreviation_expansion_does_not_rescue_a_divergent_suffix() -> None:
    """'ceramics' and 'chemicals' share a prefix but neither is a prefix of the other, so
    expansion leaves them alone. This is precisely why expansion costs no precision."""
    assert similarity("Highland Ceramics", "Highland Chemicals") == pytest.approx(85.71, abs=0.1)


# --- precedence -------------------------------------------------------------------------------


def test_gstin_exact_match_wins_absolutely() -> None:
    """Even when the names look nothing alike."""
    candidates = [_cp("Completely Different Name Pvt Ltd", "29AABCU9603R1ZM")]
    match = find_match(candidates, name="Krishna Textiles", gstin="29AABCU9603R1ZM")
    assert match is not None
    assert match.method is MatchMethod.GSTIN


def test_gstin_match_is_case_insensitive() -> None:
    candidates = [_cp("Krishna Textiles", "29AABCU9603R1ZM")]
    match = find_match(candidates, name="Unrelated", gstin="29aabcu9603r1zm")
    assert match is not None
    assert match.method is MatchMethod.GSTIN


def test_differing_gstins_block_a_name_merge() -> None:
    """Two registrations are two legal entities, however identical the trading name."""
    candidates = [_cp("Krishna Textiles", "29AABCU9603R1ZM")]
    match = find_match(candidates, name="Krishna Textiles", gstin="27AABCU9603R1ZP")
    assert match is None, "must not merge across a known GSTIN disagreement"


def test_unknown_gstin_still_allows_a_name_match() -> None:
    """An existing record may predate the merchant recording GSTINs at all."""
    candidates = [_cp("Krishna Textiles", None)]
    match = find_match(candidates, name="Krishna Textiles", gstin="29AABCU9603R1ZM")
    assert match is not None
    assert match.method is MatchMethod.FUZZY_NAME


def test_no_candidate_above_threshold_returns_none() -> None:
    candidates = [_cp("Highland Chemicals"), _cp("Anand Industries")]
    assert find_match(candidates, name="Highland Ceramics", gstin=None) is None


def test_best_candidate_wins_when_several_are_close() -> None:
    candidates = [_cp("Krishna Textile Mills"), _cp("Krishna Textiles Pvt Ltd")]
    match = find_match(candidates, name="Krishna Textiles", gstin=None)
    assert match is not None
    assert match.counterparty.name == "Krishna Textiles Pvt Ltd"


# --- multiple candidates: the pairwise-expansion gap --------------------------------------------


def test_an_abbreviation_matching_two_candidates_does_not_merge() -> None:
    """The gap the pairwise expansion rule cannot see on its own.

    "ind" is a prefix of exactly one token in "Ram Industries", and also of exactly one token in
    "Ram Indane". Checked one candidate at a time, each looks like a clean 100 match. Picking
    either is a coin flip that merges two companies' payment histories and consent records.
    """
    industries = _cp("Ram Industries")
    indane = _cp("Ram Indane")

    assert similarity("Ram Ind.", industries.name) >= MATCH_THRESHOLD
    assert similarity("Ram Ind.", indane.name) >= MATCH_THRESHOLD

    with pytest.raises(AmbiguousMatchError) as excinfo:
        find_match([industries, indane], name="Ram Ind.", gstin=None)

    # The error names both candidates, so the repair queue can show the merchant the choice.
    named = {name for name, _score in excinfo.value.candidates}
    assert named == {"Ram Industries", "Ram Indane"}


def test_one_candidate_above_threshold_still_merges() -> None:
    """The ambiguity guard must not suppress an unambiguous match."""
    industries = _cp("Ram Industries")
    unrelated = _cp("Konkan Seafoods Pvt Ltd")
    match = find_match([industries, unrelated], name="Ram Ind.", gstin=None)
    assert match is not None
    assert match.counterparty.name == "Ram Industries"


def test_gstin_exact_match_beats_an_ambiguous_name() -> None:
    """GSTIN wins absolutely -- it resolves the tie before name scoring is reached."""
    industries = _cp("Ram Industries", "29AABCU9603R1ZM")
    indane = _cp("Ram Indane")
    match = find_match([industries, indane], name="Ram Ind.", gstin="29AABCU9603R1ZM")
    assert match is not None
    assert match.method is MatchMethod.GSTIN
    assert match.counterparty.name == "Ram Industries"


def test_a_differing_gstin_disambiguates_by_elimination() -> None:
    """Two name matches, but one is ruled out by a GSTIN disagreement -- no ambiguity left."""
    industries = _cp("Ram Industries", "27AABCU9603R1ZP")
    indane = _cp("Ram Indane")
    match = find_match([industries, indane], name="Ram Ind.", gstin="29AABCU9603R1ZM")
    assert match is not None
    assert match.counterparty.name == "Ram Indane"


def test_three_way_ambiguity_is_also_refused() -> None:
    candidates = [_cp("Ram Industries"), _cp("Ram Indane"), _cp("Ram Indus Pvt Ltd")]
    with pytest.raises(AmbiguousMatchError):
        find_match(candidates, name="Ram Ind.", gstin=None)
