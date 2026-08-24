"""Rule-based column-header mapping: arbitrary spreadsheet headers -> canonical fields.

Rules first, always. A dictionary of known variants covers the formats Indian SMEs actually
export -- Tally (``Voucher No``, ``Party``), Zoho (``Invoice Number``, ``Customer Name``), Busy,
and hand-rolled Excel. Only headers that fail rule matching are candidates for the LLM fallback,
and that is **one call for the whole header row, never one per column** (agents/backend.md).

The LLM fallback is a stub here on purpose -- it lands in Phase 5. Until then unmapped headers
are simply reported, and a merchant can resolve them via POST /batches/{id}/mapping.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# --- canonical fields -------------------------------------------------------------------------

INVOICE_NUMBER = "invoice_number"
COUNTERPARTY_NAME = "counterparty_name"
GSTIN = "gstin"
AMOUNT = "amount"
OUTSTANDING = "outstanding"
ISSUE_DATE = "issue_date"
DUE_DATE = "due_date"
TERMS_DAYS = "terms_days"
PO_REF = "po_ref"
CURRENCY = "currency"
CONTACT_NAME = "contact_name"
CONTACT_EMAIL = "contact_email"
CONTACT_PHONE = "contact_phone"
CONTACT_ROLE = "contact_role"

CANONICAL_FIELDS: tuple[str, ...] = (
    INVOICE_NUMBER,
    COUNTERPARTY_NAME,
    GSTIN,
    AMOUNT,
    OUTSTANDING,
    ISSUE_DATE,
    DUE_DATE,
    TERMS_DAYS,
    PO_REF,
    CURRENCY,
    CONTACT_NAME,
    CONTACT_EMAIL,
    CONTACT_PHONE,
    CONTACT_ROLE,
)

# A row cannot become an Invoice without these four. issue_date is recoverable (see normalizer),
# everything else is optional enrichment.
REQUIRED_FIELDS: tuple[str, ...] = (INVOICE_NUMBER, COUNTERPARTY_NAME, AMOUNT, DUE_DATE)


# --- known header variants --------------------------------------------------------------------
# Keys are *normalised* headers (see normalise_header): lowercased, punctuation stripped,
# collapsed whitespace. Add variants freely; this dictionary is the cheap, deterministic path and
# every entry here is one fewer LLM call in Phase 5.

_KNOWN: dict[str, str] = {
    # invoice_number -- Tally calls it a voucher, Zoho an invoice number, most exports "Bill No"
    "invoice": INVOICE_NUMBER,
    "invoice no": INVOICE_NUMBER,
    "invoice num": INVOICE_NUMBER,
    "invoice number": INVOICE_NUMBER,
    "invoice #": INVOICE_NUMBER,
    "invoice id": INVOICE_NUMBER,
    "inv no": INVOICE_NUMBER,
    "inv number": INVOICE_NUMBER,
    "inv #": INVOICE_NUMBER,
    "bill no": INVOICE_NUMBER,
    "bill number": INVOICE_NUMBER,
    "bill #": INVOICE_NUMBER,
    "voucher no": INVOICE_NUMBER,
    "voucher number": INVOICE_NUMBER,
    "voucher #": INVOICE_NUMBER,
    "document no": INVOICE_NUMBER,
    "doc no": INVOICE_NUMBER,
    "reference no": INVOICE_NUMBER,
    "ref no": INVOICE_NUMBER,
    # counterparty_name
    "party": COUNTERPARTY_NAME,
    "party name": COUNTERPARTY_NAME,
    "partys name": COUNTERPARTY_NAME,
    "customer": COUNTERPARTY_NAME,
    "customer name": COUNTERPARTY_NAME,
    "client": COUNTERPARTY_NAME,
    "client name": COUNTERPARTY_NAME,
    "buyer": COUNTERPARTY_NAME,
    "buyer name": COUNTERPARTY_NAME,
    "debtor": COUNTERPARTY_NAME,
    "debtor name": COUNTERPARTY_NAME,
    "account": COUNTERPARTY_NAME,
    "account name": COUNTERPARTY_NAME,
    "ledger": COUNTERPARTY_NAME,
    "ledger name": COUNTERPARTY_NAME,
    "company": COUNTERPARTY_NAME,
    "company name": COUNTERPARTY_NAME,
    "billed to": COUNTERPARTY_NAME,
    "bill to": COUNTERPARTY_NAME,
    "sold to": COUNTERPARTY_NAME,
    # gstin
    "gstin": GSTIN,
    "gst no": GSTIN,
    "gst number": GSTIN,
    "gst": GSTIN,
    "gstin uin": GSTIN,
    "tax id": GSTIN,
    # amount
    "amount": AMOUNT,
    "amt": AMOUNT,
    "bill amount": AMOUNT,
    "bill amt": AMOUNT,
    "invoice amount": AMOUNT,
    "invoice amt": AMOUNT,
    "invoice value": AMOUNT,
    "total": AMOUNT,
    "total amount": AMOUNT,
    "grand total": AMOUNT,
    "net amount": AMOUNT,
    "gross amount": AMOUNT,
    "value": AMOUNT,
    "debit": AMOUNT,
    # outstanding -- the residual after partial payments
    "outstanding": OUTSTANDING,
    "outstanding amount": OUTSTANDING,
    "outstanding amt": OUTSTANDING,
    "balance": OUTSTANDING,
    "balance amount": OUTSTANDING,
    "balance due": OUTSTANDING,
    "due amount": OUTSTANDING,
    "amount due": OUTSTANDING,
    "pending": OUTSTANDING,
    "pending amount": OUTSTANDING,
    "closing balance": OUTSTANDING,
    "unpaid": OUTSTANDING,
    "unpaid amount": OUTSTANDING,
    # issue_date
    "date": ISSUE_DATE,
    "invoice date": ISSUE_DATE,
    "invoice dt": ISSUE_DATE,
    "inv date": ISSUE_DATE,
    "inv dt": ISSUE_DATE,
    "bill date": ISSUE_DATE,
    "bill dt": ISSUE_DATE,
    "voucher date": ISSUE_DATE,
    "document date": ISSUE_DATE,
    "issue date": ISSUE_DATE,
    "issued on": ISSUE_DATE,
    "posting date": ISSUE_DATE,
    # due_date
    "due date": DUE_DATE,
    "due dt": DUE_DATE,
    "duedate": DUE_DATE,
    "payment due date": DUE_DATE,
    "payment due": DUE_DATE,
    "maturity date": DUE_DATE,
    "due on": DUE_DATE,
    "expected payment date": DUE_DATE,
    # terms_days
    "terms": TERMS_DAYS,
    "term": TERMS_DAYS,
    "credit days": TERMS_DAYS,
    "credit period": TERMS_DAYS,
    "payment terms": TERMS_DAYS,
    "terms days": TERMS_DAYS,
    "net days": TERMS_DAYS,
    # po_ref
    "po": PO_REF,
    "po no": PO_REF,
    "po number": PO_REF,
    "po ref": PO_REF,
    "purchase order": PO_REF,
    "purchase order no": PO_REF,
    "order ref": PO_REF,
    "order no": PO_REF,
    # currency
    "currency": CURRENCY,
    "curr": CURRENCY,
    "ccy": CURRENCY,
    # contacts (FR-1.6)
    "contact": CONTACT_NAME,
    "contact name": CONTACT_NAME,
    "contact person": CONTACT_NAME,
    "attention": CONTACT_NAME,
    "attn": CONTACT_NAME,
    "email": CONTACT_EMAIL,
    "email id": CONTACT_EMAIL,
    "email address": CONTACT_EMAIL,
    "e mail": CONTACT_EMAIL,
    "contact email": CONTACT_EMAIL,
    "phone": CONTACT_PHONE,
    "phone no": CONTACT_PHONE,
    "phone number": CONTACT_PHONE,
    "mobile": CONTACT_PHONE,
    "mobile no": CONTACT_PHONE,
    "mobile number": CONTACT_PHONE,
    "contact no": CONTACT_PHONE,
    "contact number": CONTACT_PHONE,
    "telephone": CONTACT_PHONE,
    "role": CONTACT_ROLE,
    "designation": CONTACT_ROLE,
    "contact role": CONTACT_ROLE,
}


@dataclass(frozen=True)
class MappingResult:
    """``mapping`` is original-header -> canonical field. ``unmapped`` keeps file order."""

    mapping: dict[str, str]
    unmapped: list[str]

    @property
    def missing_required(self) -> list[str]:
        mapped = set(self.mapping.values())
        return [f for f in REQUIRED_FIELDS if f not in mapped]


def normalise_header(header: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace. ``"Due Dt."`` -> ``"due dt"``.

    ``#`` survives because ``Invoice #`` is a common and unambiguous variant.
    """
    text = header.strip().lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9# ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def map_headers(headers: list[str]) -> MappingResult:
    """Map headers to canonical fields by rule.

    First match wins for a given canonical field, so a file carrying both ``Amount`` and
    ``Bill Amount`` binds the leftmost and reports the other as unmapped rather than silently
    overwriting. Headers that match nothing are returned in ``unmapped`` for the LLM fallback
    (Phase 5) or a merchant override.
    """
    mapping: dict[str, str] = {}
    unmapped: list[str] = []
    claimed: set[str] = set()

    for header in headers:
        field = _KNOWN.get(normalise_header(header))
        if field is None or field in claimed:
            unmapped.append(header)
            continue
        mapping[header] = field
        claimed.add(field)
    return MappingResult(mapping=mapping, unmapped=unmapped)


def apply_override(
    headers: list[str], override: dict[str, str], *, strict: bool = True
) -> MappingResult:
    """Build a mapping from a merchant-supplied ``header -> canonical field`` override.

    Used by POST /batches/{id}/mapping. Unknown canonical field names are rejected rather than
    silently dropped: a mistyped field would otherwise look like a successful re-parse that
    quietly lost a column.
    """
    if strict:
        bad = sorted({v for v in override.values() if v not in CANONICAL_FIELDS})
        if bad:
            raise ValueError(
                f"unknown canonical field(s): {', '.join(bad)}; "
                f"expected one of {', '.join(CANONICAL_FIELDS)}"
            )
        unknown_headers = sorted(set(override) - set(headers))
        if unknown_headers:
            raise ValueError(f"header(s) not present in the file: {', '.join(unknown_headers)}")

    mapping = {h: override[h] for h in headers if h in override}
    unmapped = [h for h in headers if h not in mapping]
    return MappingResult(mapping=mapping, unmapped=unmapped)


def llm_map_headers(unmapped: list[str], *, sample_rows: list[dict[str, str]]) -> dict[str, str]:
    """LLM fallback for headers the rules could not place. **Not wired up -- Phase 5.**

    Contract when it lands (ADR-003, agents/backend.md):

    * exactly **one** call for the whole unmapped header row, never one per column;
    * the call takes the header names plus a few sample rows, so the model can use cell shape
      (``"31/03/2026"`` looks like a date) rather than the header name alone;
    * it returns ``header -> canonical field`` restricted to :data:`CANONICAL_FIELDS`, and
      anything outside that set is discarded by the caller, never trusted;
    * it runs off the request path -- ``POST /batches`` must not block on an LLM (backend.md
      ground rule 3). Until then the merchant resolves these via POST /batches/{id}/mapping.

    Returns an empty mapping so callers can already depend on the shape.
    """
    del unmapped, sample_rows  # referenced by the Phase 5 implementation
    return {}
