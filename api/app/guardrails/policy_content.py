"""Content policy: what a message may never say, and what it must always contain.

Rule-based and deterministic, per ADR-005. LLM moderation was rejected as the primary control --
"a compliance control a language model can be talked out of is not a control" -- and its failure
mode is silent, which is the worst property a compliance check can have. An LLM layer is
acceptable only *on top* of this one, never instead of it.

Two halves, both from architecture/agent-loop.md:

* **Banned** — legal threats, credit-rating threats, references to family or personal assets,
  disclosure to third parties, shaming language, ALL CAPS demands, fake urgency, and any claim of
  legal action not actually being taken.
* **Required** — the correct outstanding amount, the invoice number, a payment link, an opt-out
  mechanism, and sender identification.

The banned patterns are matched against normalised text (lowercased, whitespace collapsed), so
"F I N A L  N O T I C E" and "final   notice" are the same string to this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

# Tier at which "final notice" style urgency stops being fake. Below this, urgency language is
# claiming a finality the sequence has not reached.
FINAL_NOTICE_MIN_TIER = 4

# A demand is only "shouted" if it is long enough for capitalisation to be a choice rather than an
# acronym or an invoice number.
ALL_CAPS_MIN_RUN = 12
_ALL_CAPS_RE = re.compile(rf"[A-Z][A-Z\s!.,'-]{{{ALL_CAPS_MIN_RUN},}}")


class BannedCategory(StrEnum):
    LEGAL_THREAT = "legal_threat"
    CREDIT_THREAT = "credit_threat"
    PERSONAL_ASSETS = "personal_assets"
    THIRD_PARTY_DISCLOSURE = "third_party_disclosure"
    SHAMING = "shaming"
    ALL_CAPS_DEMAND = "all_caps_demand"
    FAKE_URGENCY = "fake_urgency"


@dataclass(frozen=True)
class BannedPattern:
    category: BannedCategory
    pattern: re.Pattern[str]
    description: str


def _p(category: BannedCategory, expression: str, description: str) -> BannedPattern:
    return BannedPattern(category, re.compile(expression), description)


# Phrases, not single words: "legal" alone appears innocently ("our legal entity name"), and a
# checker that fires on it trains merchants to ignore the checker.
BANNED_PATTERNS: tuple[BannedPattern, ...] = (
    # --- legal threats (RBI recovery-conduct norms; also any claim of action not being taken) ---
    _p(BannedCategory.LEGAL_THREAT, r"\blegal action\b", "threatens legal action"),
    _p(BannedCategory.LEGAL_THREAT, r"\blegal notice\b", "threatens a legal notice"),
    _p(BannedCategory.LEGAL_THREAT, r"\bcourt\b", "references court"),
    _p(BannedCategory.LEGAL_THREAT, r"\blawyer|\badvocate\b|\bsolicitor\b", "references counsel"),
    _p(BannedCategory.LEGAL_THREAT, r"\bsue\b|\bsuing\b|\blitigat", "threatens litigation"),
    _p(BannedCategory.LEGAL_THREAT, r"\bprosecut", "threatens prosecution"),
    _p(
        BannedCategory.LEGAL_THREAT,
        r"\bsection 138\b|\bnegotiable instruments act\b",
        "threatens cheque-bounce prosecution",
    ),
    _p(BannedCategory.LEGAL_THREAT, r"\binsolvency\b|\bibc\b|\bnclt\b", "threatens insolvency"),
    _p(BannedCategory.LEGAL_THREAT, r"\barbitration\b", "threatens arbitration"),
    _p(BannedCategory.LEGAL_THREAT, r"\brecovery agent", "threatens a recovery agent"),
    # --- credit-rating threats ---
    _p(
        BannedCategory.CREDIT_THREAT,
        r"\bcredit (score|rating|bureau|report)\b",
        "threatens the credit rating",
    ),
    _p(
        BannedCategory.CREDIT_THREAT,
        r"\bcibil\b|\bcrif\b|\bexperian\b|\bequifax\b",
        "threatens a credit bureau listing",
    ),
    _p(BannedCategory.CREDIT_THREAT, r"\bblacklist", "threatens blacklisting"),
    _p(
        BannedCategory.CREDIT_THREAT,
        r"\bdefaulter list\b|\bwilful defaulter\b",
        "threatens a defaulter listing",
    ),
    # --- family / personal assets ---
    _p(
        BannedCategory.PERSONAL_ASSETS,
        r"\bpersonal (asset|property|guarantee|liability)",
        "references personal assets",
    ),
    _p(
        BannedCategory.PERSONAL_ASSETS,
        r"\byour (family|wife|husband|children|home|house)\b",
        "references family or home",
    ),
    _p(
        BannedCategory.PERSONAL_ASSETS, r"\bseiz(e|ure)\b|\battach(ment)? of\b", "threatens seizure"
    ),
    # --- third-party disclosure ---
    _p(
        BannedCategory.THIRD_PARTY_DISCLOSURE,
        r"\binform your (customer|client|supplier|bank)",
        "threatens disclosure to a third party",
    ),
    _p(BannedCategory.THIRD_PARTY_DISCLOSURE, r"\bnotify your\b", "threatens third-party notice"),
    _p(
        BannedCategory.THIRD_PARTY_DISCLOSURE,
        r"\bpublicly\b|\bpublic notice\b|\bpublish\b",
        "threatens publication",
    ),
    _p(BannedCategory.THIRD_PARTY_DISCLOSURE, r"\bsocial media\b", "threatens social media"),
    _p(
        BannedCategory.THIRD_PARTY_DISCLOSURE,
        r"\btell (everyone|others|your)",
        "threatens disclosure",
    ),
    # --- shaming ---
    _p(BannedCategory.SHAMING, r"\bshame(ful|less)?\b", "shaming language"),
    _p(BannedCategory.SHAMING, r"\bembarrass", "shaming language"),
    _p(BannedCategory.SHAMING, r"\bdishonest\b|\bfraud\b|\bcheat", "accuses of dishonesty"),
    _p(BannedCategory.SHAMING, r"\birresponsible\b|\bnegligent\b", "shaming language"),
    _p(BannedCategory.SHAMING, r"\bhow can you\b|\baren'?t you ashamed\b", "shaming language"),
    # --- fake urgency (tier-dependent; see check_fake_urgency) ---
    _p(BannedCategory.FAKE_URGENCY, r"\bfinal notice\b", "claims finality"),
    _p(BannedCategory.FAKE_URGENCY, r"\blast (warning|chance|reminder)\b", "claims finality"),
    _p(BannedCategory.FAKE_URGENCY, r"\bimmediate(ly)? or\b", "manufactured ultimatum"),
    _p(BannedCategory.FAKE_URGENCY, r"\bwithin 24 hours or\b", "manufactured ultimatum"),
)

# Only these are tier-gated; everything else is banned at every tier.
_TIER_GATED = {BannedCategory.FAKE_URGENCY}


@dataclass(frozen=True)
class ContentViolation:
    category: str
    description: str
    evidence: str

    def as_dict(self) -> dict[str, str]:
        return {
            "category": self.category,
            "description": self.description,
            "evidence": self.evidence,
        }


def normalise(text: str) -> str:
    """Lowercase and collapse whitespace, so spacing tricks do not evade a pattern."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def find_banned(text: str, *, tone_tier: int) -> list[ContentViolation]:
    """Every banned phrase in the text. Returns all of them, not the first.

    A merchant fixing one violation at a time through repeated regeneration is a worse experience
    than being told everything wrong at once, and the audit entry is more useful complete.
    """
    haystack = normalise(text)
    violations: list[ContentViolation] = []
    for banned in BANNED_PATTERNS:
        if banned.category in _TIER_GATED and tone_tier >= FINAL_NOTICE_MIN_TIER:
            continue
        match = banned.pattern.search(haystack)
        if match:
            violations.append(
                ContentViolation(banned.category.value, banned.description, match.group(0))
            )
    return violations


def find_all_caps_demand(text: str) -> ContentViolation | None:
    """A shouted demand. Checked on the raw text, since normalisation destroys the evidence.

    Short runs are ignored: "GST", "INR", "INV-2026-1001" and "NEFT" are not shouting.
    """
    for run in _ALL_CAPS_RE.findall(text or ""):
        letters = [c for c in run if c.isalpha()]
        if len(letters) >= ALL_CAPS_MIN_RUN:
            return ContentViolation(
                BannedCategory.ALL_CAPS_DEMAND.value,
                "shouted demand in capitals",
                run.strip()[:60],
            )
    return None


# --- required elements ---------------------------------------------------------------------------


class RequiredElement(StrEnum):
    AMOUNT = "amount"
    INVOICE_NUMBER = "invoice_number"
    PAYMENT_LINK = "payment_link"
    OPT_OUT = "opt_out"
    SENDER_IDENTIFICATION = "sender_identification"


def _amount_appears(body: str, amount_paise: int) -> bool:
    """Whether the body states this amount, in any of the ways a person would write it.

    Accepts ``124500``, ``1,245.00``, ``1,24,500`` (Indian grouping) and a bare rupee figure, with
    or without a currency mark. Written loosely on purpose: the point is to catch a message
    quoting the *wrong* amount, not to dictate formatting to the drafting layer.
    """
    rupees = amount_paise // 100
    digits_only = re.sub(r"[^0-9]", "", body or "")
    candidates = {str(rupees), f"{rupees:,}", _indian_group(rupees)}
    if amount_paise % 100:
        candidates.add(f"{amount_paise / 100:.2f}")
        candidates.add(f"{amount_paise // 100:,}.{amount_paise % 100:02d}")
    return any(re.sub(r"[^0-9]", "", c) in digits_only for c in candidates if c)


def _indian_group(rupees: int) -> str:
    text = str(rupees)
    if len(text) <= 3:
        return text
    head, tail = text[:-3], text[-3:]
    parts: list[str] = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    parts.insert(0, head)
    return ",".join(parts) + "," + tail


def find_missing_elements(
    body: str,
    *,
    outstanding_paise: int,
    invoice_number: str,
    payment_link_url: str | None,
    opt_out_url: str | None,
    sender_name: str | None,
) -> list[RequiredElement]:
    """Which mandatory elements the message is missing (FR-7.7, FR-2.4).

    The amount is checked against the invoice's *live* outstanding, not against what the message
    claims, so a message quoting a superseded figure after a partial payment fails here.
    """
    text = body or ""
    missing: list[RequiredElement] = []

    if not _amount_appears(text, outstanding_paise):
        missing.append(RequiredElement.AMOUNT)
    if not invoice_number or invoice_number.lower() not in text.lower():
        missing.append(RequiredElement.INVOICE_NUMBER)
    if not payment_link_url or payment_link_url not in text:
        missing.append(RequiredElement.PAYMENT_LINK)
    if not opt_out_url or opt_out_url not in text:
        missing.append(RequiredElement.OPT_OUT)
    if not sender_name or sender_name.lower() not in text.lower():
        missing.append(RequiredElement.SENDER_IDENTIFICATION)
    return missing
