"""Date parsing must never guess between DD/MM and MM/DD.

agents/backend.md: "Silently misparsing dates corrupts every downstream calculation." These tests
are the guard on that rule -- the ambiguous case must route the *whole batch* to repair, not pick
a format and carry on.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.enums import RepairErrorCode
from app.ingestion.normalizer import (
    DateFormatVerdict,
    DateOrder,
    DateParseError,
    detect_date_format,
    parse_amount_paise,
    parse_date,
)

# --- format detection -------------------------------------------------------------------------


def test_day_over_12_anywhere_resolves_the_batch_to_day_first() -> None:
    decision = detect_date_format(["05/03/2026", "18/02/2026", "01/03/2026"])
    assert decision.verdict is DateFormatVerdict.RESOLVED
    assert decision.order is DateOrder.DMY
    assert "18/02/2026" in decision.dmy_evidence


def test_second_part_over_12_resolves_to_month_first() -> None:
    decision = detect_date_format(["03/18/2026", "01/05/2026"])
    assert decision.verdict is DateFormatVerdict.RESOLVED
    assert decision.order is DateOrder.MDY


def test_all_values_under_13_is_ambiguous_not_a_guess() -> None:
    """The critical case. Every part <= 12, so nothing proves an order."""
    decision = detect_date_format(["03/04/2026", "05/06/2026", "01/02/2026"])
    assert decision.verdict is DateFormatVerdict.AMBIGUOUS
    assert decision.order is DateOrder.NOT_APPLICABLE
    assert not decision.ok
    # The merchant is shown concrete offending values, not just "bad dates".
    assert decision.sample


def test_a_file_mixing_both_orders_is_conflicting() -> None:
    decision = detect_date_format(["18/02/2026", "03/18/2026"])
    assert decision.verdict is DateFormatVerdict.CONFLICTING
    assert not decision.ok
    assert decision.dmy_evidence and decision.mdy_evidence


def test_iso_only_batch_needs_no_order() -> None:
    decision = detect_date_format(["2026-03-01", "2026-04-15"])
    assert decision.ok
    assert decision.order is DateOrder.NOT_APPLICABLE


def test_impossible_date_is_not_evidence_of_an_order() -> None:
    """32/02 has both parts > 12-ish; it must not be read as proof of day-first."""
    decision = detect_date_format(["32/02/2026", "03/04/2026"])
    assert decision.verdict is DateFormatVerdict.AMBIGUOUS


# --- the four required formats ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "order", "expected"),
    [
        ("31/03/2026", DateOrder.DMY, date(2026, 3, 31)),
        ("05/03/2026", DateOrder.DMY, date(2026, 3, 5)),
        ("31-03-26", DateOrder.DMY, date(2026, 3, 31)),
        ("05-03-26", DateOrder.DMY, date(2026, 3, 5)),
        ("2026-03-31", DateOrder.DMY, date(2026, 3, 31)),
        ("2026-03-31", DateOrder.NOT_APPLICABLE, date(2026, 3, 31)),
        ("2026/03/01", DateOrder.NOT_APPLICABLE, date(2026, 3, 1)),
        ("15 Mar 2026", DateOrder.NOT_APPLICABLE, date(2026, 3, 15)),
        ("15-March-2026", DateOrder.NOT_APPLICABLE, date(2026, 3, 15)),
        ("01.03.2026", DateOrder.DMY, date(2026, 3, 1)),
    ],
)
def test_supported_formats(value: str, order: DateOrder, expected: date) -> None:
    assert parse_date(value, order) == expected


def test_the_same_string_reads_differently_under_each_order() -> None:
    """Exactly why the order must be established from evidence and never assumed."""
    assert parse_date("03/04/2026", DateOrder.DMY) == date(2026, 4, 3)
    assert parse_date("03/04/2026", DateOrder.MDY) == date(2026, 3, 4)


def test_two_digit_year_pivots_at_70() -> None:
    assert parse_date("01/01/26", DateOrder.DMY) == date(2026, 1, 1)
    assert parse_date("01/01/99", DateOrder.DMY) == date(1999, 1, 1)


def test_empty_cell_is_none_not_an_error() -> None:
    assert parse_date("", DateOrder.DMY) is None
    assert parse_date("   ", DateOrder.DMY) is None


def test_impossible_calendar_date_raises_impossible_date() -> None:
    with pytest.raises(DateParseError) as excinfo:
        parse_date("32/02/2026", DateOrder.DMY)
    assert excinfo.value.code is RepairErrorCode.IMPOSSIBLE_DATE


def test_29_feb_in_a_non_leap_year_is_impossible() -> None:
    with pytest.raises(DateParseError) as excinfo:
        parse_date("29/02/2026", DateOrder.DMY)
    assert excinfo.value.code is RepairErrorCode.IMPOSSIBLE_DATE


def test_garbage_raises_unparseable() -> None:
    with pytest.raises(DateParseError) as excinfo:
        parse_date("next Tuesday", DateOrder.DMY)
    assert excinfo.value.code is RepairErrorCode.UNPARSEABLE_DATE


def test_numeric_date_without_a_resolved_order_refuses_to_guess() -> None:
    """Defence in depth: even called directly, an ambiguous value never silently picks a format."""
    with pytest.raises(DateParseError) as excinfo:
        parse_date("03/04/2026", DateOrder.NOT_APPLICABLE)
    assert excinfo.value.code is RepairErrorCode.AMBIGUOUS_DATE_FORMAT


# --- amounts ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected_paise"),
    [
        ("245000", 24_500_000),
        ("88,000.50", 8_800_050),
        ("1,20,000", 12_000_000),  # Indian digit grouping
        ("Rs. 4500", 450_000),
        ("₹ 4,500", 450_000),
        ("INR 4500", 450_000),
        ("4500.00", 450_000),
    ],
)
def test_amount_parsing(value: str, expected_paise: int) -> None:
    assert parse_amount_paise(value) == expected_paise


def test_amount_stays_exact_in_paise() -> None:
    """Decimal, not float: 88000.50 must be exactly 8800050 paise, never 8800049."""
    assert parse_amount_paise("88,000.50") == 8_800_050


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("Rs. Twelve Thousand", RepairErrorCode.UNPARSEABLE_AMOUNT),
        ("", RepairErrorCode.MISSING_AMOUNT),
        ("-5000", RepairErrorCode.NON_POSITIVE_AMOUNT),
        ("0", RepairErrorCode.NON_POSITIVE_AMOUNT),
        ("(1200)", RepairErrorCode.NON_POSITIVE_AMOUNT),
    ],
)
def test_bad_amounts_carry_a_specific_repair_code(value: str, code: RepairErrorCode) -> None:
    with pytest.raises(Exception) as excinfo:
        parse_amount_paise(value)
    assert getattr(excinfo.value, "code", None) is code
