"""Counterparty resolution: GSTIN first, then conservative fuzzy name matching.

**A false merge is far worse than a duplicate.** Merging two companies merges their payment
histories and, worse, their consent records -- which means chasing someone who never consented.
So the threshold is deliberately high (88, per agents/backend.md) and every widening of the
matcher below is a *precision-preserving* one: it makes abbreviations resolve, never makes
similar-but-different names collide.

Order of precedence:

1. **GSTIN exact match wins absolutely.** A GSTIN is a government-issued identity; if it matches,
   name similarity is irrelevant, and if it differs the names being identical is irrelevant.
2. Otherwise normalise and score with ``rapidfuzz.token_sort_ratio`` at threshold 88.
3. Below 88, create a new counterparty.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from enum import StrEnum

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.counterparty import Counterparty

MATCH_THRESHOLD = 88

# Legal-form suffixes carry no identifying information and differ freely across exports
# ("Pvt Ltd" / "Private Limited" / omitted). agents/backend.md names this list.
_LEGAL_SUFFIXES = {
    "pvt",
    "private",
    "ltd",
    "limited",
    "llp",
    "inc",
    "co",
    "company",
    "corp",
    "corporation",
}

# Shortest token treated as a possible abbreviation. Below this ("SS", "MK") a prefix match is
# noise rather than evidence.
_MIN_ABBREV_LEN = 3


class MatchMethod(StrEnum):
    GSTIN = "gstin"
    FUZZY_NAME = "fuzzy_name"
    CREATED = "created"


class AmbiguousMatchError(Exception):
    """Two or more counterparties scored at or above threshold for the same name.

    The "expand a token that is a prefix of exactly one token in the other name" rule is
    *pairwise*: it is checked against one candidate at a time and therefore cannot see that a
    second candidate expands just as well. Given both ``Ram Industries`` and ``Ram Indane``,
    ``Ram Ind.`` expands to a 100 match against each -- and picking either is a coin flip that
    merges two companies' payment histories and consent records.

    So the tie is never broken automatically. The row goes to the repair queue and the merchant
    decides, because a false merge is not recoverable and a delay is.
    """

    def __init__(self, name: str, candidates: list[tuple[str, float]]) -> None:
        self.name = name
        self.candidates = candidates
        rendered = ", ".join(f"{n!r} ({score:.0f})" for n, score in candidates)
        super().__init__(f"{name!r} matches more than one counterparty: {rendered}")


@dataclass(frozen=True)
class MatchResult:
    counterparty: Counterparty
    method: MatchMethod
    score: float | None = None
    matched_against: str | None = None

    @property
    def created(self) -> bool:
        return self.method is MatchMethod.CREATED


def _tokens(name: str) -> list[str]:
    """Split a company name into comparable tokens, dropping legal-form suffixes."""
    text = name.lower().replace("&", " ")
    out: list[str] = []
    for part in re.split(r"[\s,/_-]+", text):
        cleaned = re.sub(r"[^a-z0-9]", "", part)
        if cleaned and cleaned not in _LEGAL_SUFFIXES:
            out.append(cleaned)
    return out


def normalize_name(name: str) -> str:
    """Lowercase, drop legal suffixes and punctuation, collapse whitespace.

    This is what lands in ``counterparties.name_normalized`` and what ``idx_cp_match`` indexes.
    """
    return " ".join(_tokens(name))


def _expand_abbreviations(source: list[str], against: list[str]) -> list[str]:
    """Expand abbreviated tokens in ``source`` to their full form in ``against``.

    ``"Sundaram Auto Comp."`` vs ``"Sundaram Auto Components Pvt Ltd"`` scores only 85.71 on raw
    ``token_sort_ratio`` -- below threshold -- because ``comp`` and ``components`` are simply
    different tokens. FR-1.4 requires that pair to match, and requires ``"Sharma Ent."`` to match
    ``"Sharma Enterprises Pvt Ltd"`` (a mere 71.43 raw).

    Expansion only fires when a token is a strict prefix of **exactly one** token in the other
    name. That is the shape of a genuine abbreviation, and it is why this does not cost
    precision: ``ceramics``/``chemicals`` and ``traders``/``trading`` share a prefix but neither
    is a prefix of the other, so they are left alone and stay below threshold.
    """
    expanded: list[str] = []
    for token in source:
        if len(token) >= _MIN_ABBREV_LEN:
            candidates = [t for t in against if t != token and t.startswith(token)]
            if len(candidates) == 1:
                expanded.append(candidates[0])
                continue
        expanded.append(token)
    return expanded


def similarity(left: str, right: str) -> float:
    """Similarity of two company names, 0-100, abbreviation-aware.

    Takes the better of the raw ``token_sort_ratio`` and the score after mutual abbreviation
    expansion, so expansion can only ever rescue a true match, never suppress one.
    """
    left_tokens, right_tokens = _tokens(left), _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0

    raw = fuzz.token_sort_ratio(" ".join(left_tokens), " ".join(right_tokens))
    expanded = fuzz.token_sort_ratio(
        " ".join(_expand_abbreviations(left_tokens, right_tokens)),
        " ".join(_expand_abbreviations(right_tokens, left_tokens)),
    )
    return float(max(raw, expanded))


def find_match(
    candidates: list[Counterparty], *, name: str, gstin: str | None
) -> MatchResult | None:
    """Best match among ``candidates``, or ``None`` to create a new counterparty.

    Pure function over an in-memory candidate list so the caller can load a merchant's
    counterparties once per batch instead of querying per row.
    """
    if gstin:
        for candidate in candidates:
            if candidate.gstin and candidate.gstin.upper() == gstin.upper():
                return MatchResult(candidate, MatchMethod.GSTIN, matched_against=candidate.name)
        # A GSTIN that matches nothing is not a reason to skip name matching: the existing record
        # may predate the merchant recording GSTINs at all.

    # Score every candidate, then look at *all* of the ones above threshold -- not just the best.
    # Taking the argmax would silently resolve a genuine tie.
    scored = [(candidate, similarity(name, candidate.name)) for candidate in candidates]
    over_threshold = [(candidate, score) for candidate, score in scored if score >= MATCH_THRESHOLD]

    if not over_threshold:
        return None

    # A known GSTIN disagreement rules a candidate out entirely: two registrations are two legal
    # entities however identical the trading names look.
    if gstin:
        over_threshold = [
            (candidate, score)
            for candidate, score in over_threshold
            if not (candidate.gstin and candidate.gstin.upper() != gstin.upper())
        ]
        if not over_threshold:
            return None

    if len(over_threshold) > 1:
        raise AmbiguousMatchError(
            name, sorted(((c.name, s) for c, s in over_threshold), key=lambda x: -x[1])
        )

    best, best_score = over_threshold[0]
    return MatchResult(best, MatchMethod.FUZZY_NAME, best_score, best.name)


def resolve_counterparty(
    db: Session,
    *,
    merchant_id: uuid.UUID,
    name: str,
    gstin: str | None = None,
    candidates: list[Counterparty] | None = None,
) -> MatchResult:
    """Resolve a counterparty name to an existing record, or create one.

    ``candidates`` lets a batch load the merchant's counterparties once; when omitted they are
    queried. Either way the query is scoped to ``merchant_id`` -- a match must never cross a
    tenant boundary.

    Raises :class:`AmbiguousMatchError` when more than one candidate is at or above threshold.
    The caller routes that row to the repair queue; nothing is created, because inventing a
    counterparty for a row that never becomes an invoice leaves an orphan record carrying a
    quarantine flag the merchant never asked for.
    """
    if candidates is None:
        candidates = list(
            db.execute(
                select(Counterparty).where(Counterparty.merchant_id == merchant_id)
            ).scalars()
        )

    match = find_match(candidates, name=name, gstin=gstin)
    if match is not None:
        # Backfill a GSTIN we did not previously know for this counterparty.
        if gstin and not match.counterparty.gstin:
            match.counterparty.gstin = gstin
        return match

    counterparty = Counterparty(
        id=uuid.uuid4(),
        merchant_id=merchant_id,
        name=name.strip(),
        name_normalized=normalize_name(name),
        gstin=gstin,
        # FR-2.3: a new counterparty has no consent basis yet, so it is quarantined until the
        # merchant confirms one. Never contacted while quarantined.
        is_quarantined=True,
    )
    db.add(counterparty)
    db.flush()
    candidates.append(counterparty)
    return MatchResult(counterparty, MatchMethod.CREATED)
