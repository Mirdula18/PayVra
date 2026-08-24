"""Curated, static seed inputs: archetypes and realistic Indian B2B counterparty names.

Distribution matters more than volume — every archetype exercises a different branch of the
agent's diagnosis logic (agents/data-and-seed.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field

RANDOM_SEED = 20260823

# The merchant is PAYVRA's user — an SME distributor whose AR lead is Priya (docs/vision.md).
MERCHANT_NAME = "Nandi Industrial Supplies Pvt Ltd"
MERCHANT_EMAIL = "priya@nandisupplies.co.in"

# The Hinglish reply is a scripted demo moment; it must be verbatim in the seed, not typed live.
HINGLISH_REPLY = "bhai next Tuesday tak clear kar dunga, GST invoice bhejo"

# The seven guardrail checks, in gate order. Blocked seed entries fail exactly one.
GATE_CHECKS = (
    "consent",
    "time_window",
    "frequency_cap",
    "freshness",
    "content_policy",
    "stopping_rules",
    "approval_threshold",
)


@dataclass(frozen=True)
class Archetype:
    key: str
    behaviour: str
    # Typical days-to-pay window; also biases how aged the open invoices are.
    pay_days: tuple[int, int]
    names: list[str] = field(default_factory=list)


# Counts sum to 34. Names are distinct and deliberately Indian; no "Acme Corp" / "Test Company".
ARCHETYPES: list[Archetype] = [
    Archetype(
        "reliable_late",
        "Pays in 35-50 days, always pays; oversight, tier 1 only",
        (35, 50),
        [
            "Sundaram Auto Components Pvt Ltd",
            "Krishna Textiles",
            "Anand Enterprises",
            "Deccan Steel Traders Pvt Ltd",
            "Godavari Packaging LLP",
            "Sri Lakshmi Agencies",
            "Patel Hardware & Tools",
            "Coromandel Chemicals Pvt Ltd",
            "Nirmal Plastics",
            "Venkatesh Electricals",
            "Bombay Fasteners Pvt Ltd",
            "Ashirwad Industrial Supplies",
        ],
    ),
    Archetype(
        "chronic_slow",
        "Pays in 75-95 days, needs chasing; full sequence",
        (75, 95),
        [
            "Meridian Logistics LLP",
            "Rajputana Cement Distributors",
            "Kaveri Paper Mills Pvt Ltd",
            "Sharma Engineering Works",
            "Eastern Rubber Products",
            "Gujarat Polymers Pvt Ltd",
            "Highland Ceramics",
            "Surya Pipes & Fittings",
        ],
    ),
    Archetype(
        "cash_crunched",
        "Opens links repeatedly, pays partially; cash_crunch, instalment path",
        (60, 110),
        [
            "Bright Star Garments",
            "Konkan Seafoods Pvt Ltd",
            "Newage Automotive Parts",
            "Sai Balaji Distributors",
            "Frontier Packaging Co",
        ],
    ),
    Archetype(
        "promise_breaker",
        "Promises, misses, promises again; PTP + broken-promise escalation",
        (80, 120),
        [
            "Zenith Marketing Pvt Ltd",
            "Royal Timber Traders",
            "Apex Interiors LLP",
        ],
    ),
    Archetype(
        "disputer",
        "Replies with a genuine dispute; freeze + human routing",
        (50, 90),
        [
            "Pinnacle Infra Projects Pvt Ltd",
            "Orient Glass Works",
        ],
    ),
    Archetype(
        "wrong_contact",
        "Emails bounce; wrong_contact, channel switch",
        (40, 80),
        [
            "Sterling Components Pvt Ltd",
            "Maple Retail Ventures",
        ],
    ),
    Archetype(
        "ghost",
        "Zero engagement ever; touch cap, exception list",
        (90, 160),
        [
            "Blue Ocean Exports",
            "Horizon Traders",
        ],
    ),
]

# Counterparties (by exact name) that are MSME — drives the MSME Act 45-day flag. Chosen because
# each carries aged invoices, so 4 invoices can cross the 45-day threshold.
MSME_NAMES = frozenset(
    {
        "Kaveri Paper Mills Pvt Ltd",
        "Sharma Engineering Works",
        "Konkan Seafoods Pvt Ltd",
        "Sai Balaji Distributors",
    }
)

# Counterparties that prefer Hinglish messaging (the Hinglish promise comes from Royal Timber).
HINGLISH_NAMES = frozenset({"Royal Timber Traders", "Konkan Seafoods Pvt Ltd", "Krishna Textiles"})

# The counterparty the Hinglish promise-to-pay reply is attributed to.
HINGLISH_REPLY_NAME = "Royal Timber Traders"

# Merchant opted this account out of automation; agent must never contact it.
EXCLUDED_NAME = "Maple Retail Ventures"

# No consent basis on file -> quarantined, cannot be contacted until resolved.
QUARANTINED_NAME = "Blue Ocean Exports"

# Deliberate name variant shipped in the messy upload fixture so the fuzzy matcher is exercised
# at ingestion (Phase 1). The canonical counterparty is the first entry above.
NAME_VARIANT = ("Sundaram Auto Components Pvt Ltd", "Sundaram Auto Comp.")
