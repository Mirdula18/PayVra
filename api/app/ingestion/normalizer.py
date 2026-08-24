"""Raw row -> canonical Invoice fields, with validation and repair-queue routing.

The hard rule, from agents/backend.md: **never guess between DD/MM and MM/DD.** Indian exports
use both, and silently misparsing a date corrupts every downstream calculation -- aging, the MSME
45-day flag, the whole worklist. So format is resolved once for the *whole batch* by evidence
(:func:`detect_date_format`), and if the evidence is insufficient the entire batch goes to the
repair queue and we ask. There is no fallback that picks one.

Row-level validation failures are not exceptions. They become ``batch_rows`` with a
:class:`~app.enums.RepairErrorCode`, so the merchant sees exactly what came out of their file.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from app.enums import RepairErrorCode
from app.ingestion import mapper

# --- date format ------------------------------------------------------------------------------


class DateOrder(StrEnum):
    """How to read a two-part-first numeric date within this batch."""

    DMY = "dmy"
    MDY = "mdy"
    # No numeric date evidence at all, or every value is self-describing (ISO). Rows still parse;
    # there is simply nothing ambiguous to resolve.
    NOT_APPLICABLE = "not_applicable"


class DateFormatVerdict(StrEnum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"  # every value <= 12 in both positions -- genuinely undecidable
    CONFLICTING = "conflicting"  # some rows prove DMY, others prove MDY -- a broken file


@dataclass(frozen=True)
class DateFormatDecision:
    verdict: DateFormatVerdict
    order: DateOrder
    # Evidence, surfaced to the merchant so the repair prompt is concrete rather than "bad dates".
    dmy_evidence: list[str] = field(default_factory=list)
    mdy_evidence: list[str] = field(default_factory=list)
    sample: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.verdict is DateFormatVerdict.RESOLVED


# 2026-03-01 or 2026/03/01 -- year first is self-describing, never ambiguous.
_ISO_RE = re.compile(r"^\s*(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\s*$")
# 01/03/2026, 1-3-26, 01.03.2026 -- the ambiguous shape.
_NUMERIC_RE = re.compile(r"^\s*(\d{1,2})[-/.](\d{1,2})[-/.](\d{2}|\d{4})\s*$")
# 15 Mar 2026 / 15-March-2026 -- month name is self-describing.
_NAMED_RE = re.compile(r"^\s*(\d{1,2})[-\s/]+([A-Za-z]{3,9})[-\s/,]+(\d{2}|\d{4})\s*$")
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_name) if m})


def _expand_year(year: int) -> int:
    """Two-digit years: 70-99 -> 19xx, 00-69 -> 20xx. Invoices are never 1900s in practice."""
    if year >= 100:
        return year
    return 1900 + year if year >= 70 else 2000 + year


def detect_date_format(values: list[str]) -> DateFormatDecision:
    """Resolve DD/MM vs MM/DD for a batch from the evidence in its own date columns.

    Any first-position value > 12 proves day-first; any second-position value > 12 proves
    month-first. If both appear the file is internally inconsistent (``CONFLICTING``); if neither
    appears the batch is genuinely undecidable (``AMBIGUOUS``). Both route the whole batch to the
    repair queue -- we ask rather than guess.
    """
    dmy_evidence: list[str] = []
    mdy_evidence: list[str] = []
    numeric_samples: list[str] = []

    for value in values:
        text = (value or "").strip()
        if not text:
            continue
        match = _NUMERIC_RE.match(text)
        if not match:
            continue
        numeric_samples.append(text)
        first, second = int(match.group(1)), int(match.group(2))
        # Evidence must be a value that is a valid *day* but impossible as a month: 13-31.
        # 32 proves nothing -- it is not a valid day in either order, just a broken cell -- and
        # letting it "resolve" the batch would decide the whole file's format from a typo.
        first_is_day_only = 12 < first <= 31
        second_is_day_only = 12 < second <= 31
        if first_is_day_only and second <= 12:
            dmy_evidence.append(text)
        elif second_is_day_only and first <= 12:
            mdy_evidence.append(text)
        # Anything else (both <= 12, or an out-of-range part) contributes no evidence. Impossible
        # dates are caught per-row by parse_date.

    if not numeric_samples:
        # Only ISO / month-name / empty values: nothing ambiguous exists to resolve.
        return DateFormatDecision(DateFormatVerdict.RESOLVED, DateOrder.NOT_APPLICABLE)
    if dmy_evidence and mdy_evidence:
        return DateFormatDecision(
            DateFormatVerdict.CONFLICTING,
            DateOrder.NOT_APPLICABLE,
            dmy_evidence=dmy_evidence[:5],
            mdy_evidence=mdy_evidence[:5],
            sample=numeric_samples[:5],
        )
    if dmy_evidence:
        return DateFormatDecision(
            DateFormatVerdict.RESOLVED, DateOrder.DMY, dmy_evidence=dmy_evidence[:5]
        )
    if mdy_evidence:
        return DateFormatDecision(
            DateFormatVerdict.RESOLVED, DateOrder.MDY, mdy_evidence=mdy_evidence[:5]
        )
    return DateFormatDecision(
        DateFormatVerdict.AMBIGUOUS, DateOrder.NOT_APPLICABLE, sample=numeric_samples[:5]
    )


class DateParseError(ValueError):
    """A single value could not be read as a date under the batch's resolved format."""

    def __init__(self, code: RepairErrorCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def parse_date(value: str, order: DateOrder) -> date | None:
    """Parse one date cell under the batch's resolved ``order``. ``None`` for an empty cell.

    Raises :class:`DateParseError` for a value that cannot be read -- an impossible calendar date
    (``32/02/2026``) or a shape inconsistent with the rest of the batch (an ISO-with-slashes value
    in an otherwise DD/MM column).
    """
    text = (value or "").strip()
    if not text:
        return None

    iso = _ISO_RE.match(text)
    if iso:
        year, month, day = int(iso.group(1)), int(iso.group(2)), int(iso.group(3))
        return _build_date(year, month, day, text)

    named = _NAMED_RE.match(text)
    if named:
        month = _MONTHS.get(named.group(2).lower(), 0)
        if not month:
            raise DateParseError(
                RepairErrorCode.UNPARSEABLE_DATE, f"unrecognised month name in {text!r}"
            )
        return _build_date(_expand_year(int(named.group(3))), month, int(named.group(1)), text)

    numeric = _NUMERIC_RE.match(text)
    if numeric:
        first, second = int(numeric.group(1)), int(numeric.group(2))
        year = _expand_year(int(numeric.group(3)))
        if order is DateOrder.MDY:
            month, day = first, second
        elif order is DateOrder.DMY:
            day, month = first, second
        else:
            # Unreachable via the pipeline: a numeric value means detect_date_format resolved an
            # order or sent the batch to repair. Defensive, and never a silent guess.
            raise DateParseError(
                RepairErrorCode.AMBIGUOUS_DATE_FORMAT,
                f"{text!r} needs a batch date format that was never resolved",
            )
        return _build_date(year, month, day, text)

    raise DateParseError(RepairErrorCode.UNPARSEABLE_DATE, f"could not read {text!r} as a date")


def _build_date(year: int, month: int, day: int, original: str) -> date:
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise DateParseError(
            RepairErrorCode.IMPOSSIBLE_DATE, f"{original!r} is not a real date ({exc})"
        ) from exc


# --- money ------------------------------------------------------------------------------------

_AMOUNT_CLEAN_RE = re.compile(r"[₹$,\s]|(?i:inr|rs\.?)")
_AMOUNT_VALID_RE = re.compile(r"^-?\d+(\.\d+)?$")


class AmountParseError(ValueError):
    def __init__(self, code: RepairErrorCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def parse_amount_paise(value: str, *, allow_zero: bool = False) -> int:
    """Parse a money cell to integer paise.

    Handles ``₹1,20,000``, ``88,000.50``, ``Rs. 4500``, ``(1200)`` accounting negatives. Rejects
    anything that is not fully numeric after cleaning -- ``"Rs. Twelve Thousand"`` must land in
    the repair queue, never become a zero.

    ``allow_zero`` is for the *outstanding* column, where 0 is a meaningful value: the invoice is
    fully paid. An invoice *amount* of 0 is always a defect.

    ``Decimal`` is used for the rupee->paise conversion so ``88000.50`` is exactly 8800050 paise;
    the result is an ``int`` and money stays integer paise everywhere downstream.
    """
    text = (value or "").strip()
    if not text:
        raise AmountParseError(RepairErrorCode.MISSING_AMOUNT, "amount is empty")

    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]

    cleaned = _AMOUNT_CLEAN_RE.sub("", text)
    if not cleaned or not _AMOUNT_VALID_RE.match(cleaned):
        raise AmountParseError(
            RepairErrorCode.UNPARSEABLE_AMOUNT, f"could not read {value!r} as an amount"
        )
    try:
        rupees = Decimal(cleaned)
    except InvalidOperation as exc:
        raise AmountParseError(
            RepairErrorCode.UNPARSEABLE_AMOUNT, f"could not read {value!r} as an amount"
        ) from exc

    if negative:
        rupees = -rupees
    paise = int((rupees * 100).to_integral_value())
    if paise < 0 or (paise == 0 and not allow_zero):
        raise AmountParseError(
            RepairErrorCode.NON_POSITIVE_AMOUNT, f"amount {value!r} must be greater than zero"
        )
    return paise


# --- GSTIN ------------------------------------------------------------------------------------

# 2 state digits, 10-char PAN, 1 entity digit, 'Z', 1 checksum char.
_GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")


def normalise_gstin(value: str) -> str | None:
    """Upper-case and strip a GSTIN. ``None`` for an empty cell; raises for a malformed one."""
    text = (value or "").strip().upper().replace(" ", "")
    if not text:
        return None
    if not _GSTIN_RE.match(text):
        raise ValueError(f"{value!r} is not a valid 15-character GSTIN")
    return text


# --- row normalisation ------------------------------------------------------------------------


@dataclass
class NormalizedRow:
    """One raw row resolved to canonical Invoice fields, or an error explaining why not."""

    row_number: int
    raw: dict[str, str]
    invoice_number: str = ""
    counterparty_name: str = ""
    gstin: str | None = None
    amount_paise: int = 0
    outstanding_paise: int = 0
    issue_date: date | None = None
    due_date: date | None = None
    terms_days: int = 0
    po_ref: str | None = None
    currency: str = "INR"
    contact_name: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    contact_role: str | None = None
    error_code: str | None = None
    error_detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.error_code is None

    def fail(self, code: RepairErrorCode, detail: str) -> NormalizedRow:
        self.error_code = code.value
        self.error_detail = detail
        return self


def _get(raw: dict[str, str], mapping: dict[str, str], field_name: str) -> str:
    for header, canonical in mapping.items():
        if canonical == field_name:
            return (raw.get(header) or "").strip()
    return ""


def normalize_row(
    raw: dict[str, str],
    *,
    row_number: int,
    mapping: dict[str, str],
    order: DateOrder,
) -> NormalizedRow:
    """Resolve one raw row. Never raises for bad data -- it returns a row carrying an error code.

    Validation order is deliberate: identity first (invoice number, counterparty), then money,
    then dates, then cross-field consistency. The merchant sees the *first* thing wrong with the
    row, which is the one they can act on.
    """
    row = NormalizedRow(row_number=row_number, raw=dict(raw))

    row.invoice_number = _get(raw, mapping, mapper.INVOICE_NUMBER)
    if not row.invoice_number:
        return row.fail(RepairErrorCode.MISSING_INVOICE_NUMBER, "invoice number is blank")

    row.counterparty_name = _get(raw, mapping, mapper.COUNTERPARTY_NAME)
    if not row.counterparty_name:
        return row.fail(RepairErrorCode.MISSING_COUNTERPARTY, "customer name is blank")

    try:
        row.amount_paise = parse_amount_paise(_get(raw, mapping, mapper.AMOUNT))
    except AmountParseError as exc:
        return row.fail(exc.code, exc.detail)

    outstanding_raw = _get(raw, mapping, mapper.OUTSTANDING)
    if outstanding_raw:
        try:
            # 0 is legitimate here: a fully paid invoice still appears on an export.
            row.outstanding_paise = parse_amount_paise(outstanding_raw, allow_zero=True)
        except AmountParseError as exc:
            return row.fail(exc.code, f"outstanding: {exc.detail}")
    else:
        row.outstanding_paise = row.amount_paise

    due_raw = _get(raw, mapping, mapper.DUE_DATE)
    if not due_raw:
        return row.fail(RepairErrorCode.MISSING_DUE_DATE, "due date is blank")
    try:
        row.due_date = parse_date(due_raw, order)
        row.issue_date = parse_date(_get(raw, mapping, mapper.ISSUE_DATE), order)
    except DateParseError as exc:
        return row.fail(exc.code, exc.detail)

    gstin_raw = _get(raw, mapping, mapper.GSTIN)
    if gstin_raw:
        try:
            row.gstin = normalise_gstin(gstin_raw)
        except ValueError as exc:
            return row.fail(RepairErrorCode.INVALID_GSTIN, str(exc))

    # issue_date is recoverable: FR-1.3 only makes due_date and amount mandatory. A file with no
    # invoice date is treated as issued on its due date (terms 0) rather than sent to repair.
    if row.issue_date is None:
        row.issue_date = row.due_date
    assert row.due_date is not None and row.issue_date is not None  # narrowed by the checks above
    if row.due_date < row.issue_date:
        return row.fail(
            RepairErrorCode.DUE_BEFORE_ISSUE,
            f"due date {row.due_date.isoformat()} is before issue date "
            f"{row.issue_date.isoformat()}",
        )

    terms_raw = _get(raw, mapping, mapper.TERMS_DAYS)
    if terms_raw.isdigit():
        row.terms_days = int(terms_raw)
    else:
        row.terms_days = (row.due_date - row.issue_date).days

    row.po_ref = _get(raw, mapping, mapper.PO_REF) or None
    row.currency = (_get(raw, mapping, mapper.CURRENCY) or "INR").upper()[:3]
    row.contact_name = _get(raw, mapping, mapper.CONTACT_NAME) or None
    row.contact_email = _get(raw, mapping, mapper.CONTACT_EMAIL) or None
    row.contact_phone = _get(raw, mapping, mapper.CONTACT_PHONE) or None
    row.contact_role = _get(raw, mapping, mapper.CONTACT_ROLE) or None
    return row


def date_values_for_detection(rows: list[dict[str, str]], mapping: dict[str, str]) -> list[str]:
    """Every value from every mapped date column -- the evidence pool for format detection.

    Pooled across issue_date and due_date deliberately: one export writes both columns the same
    way, so a day > 12 anywhere in the file resolves the whole file.
    """
    headers = [h for h, c in mapping.items() if c in (mapper.ISSUE_DATE, mapper.DUE_DATE)]
    return [row.get(h, "") for row in rows for h in headers]
