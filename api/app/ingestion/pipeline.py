"""Batch ingestion orchestration: file bytes -> batches, batch_rows, counterparties, invoices.

Sits between the router and the four ingestion primitives (parsers, mapper, normalizer, matcher)
so the router stays thin and the whole flow is testable without HTTP.

Two rules here carry real consequences and are implemented literally:

* **Ambiguous dates fail the whole batch, not the row.** If DD/MM cannot be told from MM/DD,
  every row goes to the repair queue with the same code. Half-importing a file under a guessed
  format is the worst possible outcome.
* **Duplicate invoices update money only.** On ``(merchant_id, invoice_number)`` conflict we
  touch ``outstanding_paise`` and ``payment_status`` and nothing else. Overwriting
  ``recovery_state``, ``touch_count`` or ``current_tone_tier`` would reset an in-flight dunning
  sequence -- the customer would start getting tier-1 nudges again after a tier-3 escalation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import today
from app.enums import BatchRowStatus, BatchStatus, PaymentStatus, RepairErrorCode
from app.ingestion import mapper, matcher, normalizer
from app.ingestion.parsers import ParsedFile
from app.models.batch import Batch
from app.models.batch_row import BatchRow
from app.models.contact import Contact
from app.models.counterparty import Counterparty
from app.models.invoice import Invoice
from app.scoring.aging import MSME_THRESHOLD_DAYS, bucket_of, refresh_aging


@dataclass
class IngestOutcome:
    """Counters for the POST /batches response.

    The ``counterparties_*`` figures count **distinct counterparties**, not rows, per
    architecture/api-contracts.md. Counting per row made them track the invoice count instead
    (34 created + 86 matched = 120 invoices), which is meaningless: a 34-counterparty file
    cannot match 86 counterparties. Sets are used rather than a running total because most rows
    resolve to a counterparty an earlier row already handled.
    """

    created: int = 0
    updated: int = 0
    duplicates: int = 0
    repairs: int = 0
    total_outstanding_paise: int = 0
    unmapped_headers: list[str] = field(default_factory=list)

    # Disjoint by construction: a counterparty this batch created is never also "matched",
    # even though later rows in the same file do match against it.
    _matched_ids: set[uuid.UUID] = field(default_factory=set)
    _created_ids: set[uuid.UUID] = field(default_factory=set)
    _quarantined_ids: set[uuid.UUID] = field(default_factory=set)

    @property
    def counterparties_matched(self) -> int:
        """Distinct pre-existing counterparties this batch resolved to."""
        return len(self._matched_ids)

    @property
    def counterparties_created(self) -> int:
        """Distinct counterparties this batch created."""
        return len(self._created_ids)

    @property
    def counterparties_quarantined(self) -> int:
        """Distinct counterparties awaiting a consent basis (FR-2.3)."""
        return len(self._quarantined_ids)

    def record_counterparty(self, counterparty: Counterparty, *, created: bool) -> None:
        if created:
            self._created_ids.add(counterparty.id)
            if counterparty.is_quarantined:
                self._quarantined_ids.add(counterparty.id)
        elif counterparty.id not in self._created_ids:
            self._matched_ids.add(counterparty.id)


def _fail_whole_batch(
    db: Session,
    batch: Batch,
    parsed: ParsedFile,
    *,
    code: RepairErrorCode,
    detail: str,
) -> IngestOutcome:
    """Route every row of the batch to the repair queue under one shared reason."""
    for offset, raw in enumerate(parsed.rows):
        db.add(
            BatchRow(
                id=uuid.uuid4(),
                batch_id=batch.id,
                # +2: the header occupies row 1 in the merchant's spreadsheet.
                row_number=offset + 2,
                raw=raw,
                status=BatchRowStatus.REPAIR_NEEDED.value,
                error_code=code.value,
                error_detail=detail,
            )
        )
    batch.repair_count = len(parsed.rows)
    batch.row_count = len(parsed.rows)
    batch.status = BatchStatus.AWAITING_REPAIR.value
    db.flush()
    return IngestOutcome(repairs=len(parsed.rows))


def ingest_rows(
    db: Session,
    *,
    batch: Batch,
    parsed: ParsedFile,
    mapping: dict[str, str],
) -> IngestOutcome:
    """Normalise, match, and persist every row of a parsed file under an agreed column mapping.

    The batch's date format is resolved once, from the pooled evidence of all its date columns,
    before any row is read.
    """
    outcome = IngestOutcome()
    result = mapper.MappingResult(
        mapping=mapping, unmapped=[h for h in parsed.headers if h not in mapping]
    )
    outcome.unmapped_headers = result.unmapped

    missing = result.missing_required
    if missing:
        return _fail_whole_batch(
            db,
            batch,
            parsed,
            code=RepairErrorCode.UNMAPPED_REQUIRED_COLUMN,
            detail=(
                f"no column mapped to {', '.join(missing)}. "
                f"Unrecognised headers: {', '.join(result.unmapped) or 'none'}. "
                "Set the mapping via POST /batches/{batch_id}/mapping."
            ),
        )

    decision = normalizer.detect_date_format(
        normalizer.date_values_for_detection(parsed.rows, mapping)
    )
    if not decision.ok:
        if decision.verdict is normalizer.DateFormatVerdict.CONFLICTING:
            detail = (
                "this file mixes day-first and month-first dates "
                f"(day-first: {', '.join(decision.dmy_evidence)}; "
                f"month-first: {', '.join(decision.mdy_evidence)}). "
                "Re-export with one consistent date format."
            )
        else:
            detail = (
                "cannot tell DD/MM from MM/DD: every date in this file has both parts <= 12 "
                f"(e.g. {', '.join(decision.sample)}). "
                "Confirm the format, or re-export dates as YYYY-MM-DD."
            )
        return _fail_whole_batch(
            db, batch, parsed, code=RepairErrorCode.AMBIGUOUS_DATE_FORMAT, detail=detail
        )

    # One query for the merchant's counterparties, reused for every row's fuzzy match.
    candidates = list(
        db.execute(
            select(Counterparty).where(Counterparty.merchant_id == batch.merchant_id)
        ).scalars()
    )
    # Existing invoice numbers for this merchant, so a duplicate is detected without a per-row
    # query. Scoped to the merchant: invoice numbers collide freely across tenants.
    existing: dict[str, Invoice] = {
        inv.invoice_number: inv
        for inv in db.execute(
            select(Invoice).where(Invoice.merchant_id == batch.merchant_id)
        ).scalars()
    }
    seen_in_file: set[str] = set()

    for offset, raw in enumerate(parsed.rows):
        row_number = offset + 2
        row = normalizer.normalize_row(
            raw, row_number=row_number, mapping=mapping, order=decision.order
        )
        batch_row = BatchRow(id=uuid.uuid4(), batch_id=batch.id, row_number=row_number, raw=raw)

        if not row.ok:
            batch_row.status = BatchRowStatus.REPAIR_NEEDED.value
            batch_row.error_code = row.error_code
            batch_row.error_detail = row.error_detail
            outcome.repairs += 1
            db.add(batch_row)
            continue

        try:
            match = matcher.resolve_counterparty(
                db,
                merchant_id=batch.merchant_id,
                name=row.counterparty_name,
                gstin=row.gstin,
                candidates=candidates,
            )
        except matcher.AmbiguousMatchError as exc:
            # Two or more counterparties are equally good matches. Never guess -- a false merge
            # combines payment histories and consent records and cannot be undone.
            batch_row.status = BatchRowStatus.REPAIR_NEEDED.value
            batch_row.error_code = RepairErrorCode.AMBIGUOUS_COUNTERPARTY.value
            batch_row.error_detail = str(exc)
            outcome.repairs += 1
            db.add(batch_row)
            continue
        outcome.record_counterparty(match.counterparty, created=match.created)

        _upsert_contact(db, match.counterparty, row)

        invoice = existing.get(row.invoice_number)
        if invoice is not None:
            # Duplicate. Money moves; recovery state does not.
            invoice.outstanding_paise = row.outstanding_paise
            invoice.payment_status = _status_for(row.amount_paise, row.outstanding_paise)
            outcome.duplicates += 1
            if row.invoice_number not in seen_in_file:
                outcome.updated += 1
            batch_row.status = BatchRowStatus.OK.value
            batch_row.invoice_id = invoice.id
        else:
            invoice = Invoice(
                id=uuid.uuid4(),
                merchant_id=batch.merchant_id,
                counterparty_id=match.counterparty.id,
                invoice_number=row.invoice_number,
                amount_paise=row.amount_paise,
                outstanding_paise=row.outstanding_paise,
                currency=row.currency,
                issue_date=row.issue_date,
                due_date=row.due_date,
                terms_days=row.terms_days,
                po_ref=row.po_ref,
                payment_status=_status_for(row.amount_paise, row.outstanding_paise),
                **_aging_snapshot(row, is_msme=match.counterparty.is_msme),
            )
            db.add(invoice)
            db.flush()
            existing[row.invoice_number] = invoice
            outcome.created += 1
            batch_row.status = BatchRowStatus.OK.value
            batch_row.invoice_id = invoice.id

        seen_in_file.add(row.invoice_number)
        outcome.total_outstanding_paise += row.outstanding_paise
        db.add(batch_row)

    batch.row_count = len(parsed.rows)
    batch.created_count = outcome.created
    batch.updated_count = outcome.updated
    batch.duplicate_count = outcome.duplicates
    batch.repair_count = outcome.repairs
    batch.column_mapping = dict(mapping)
    batch.status = (
        BatchStatus.AWAITING_REPAIR.value if outcome.repairs else BatchStatus.COMPLETE.value
    )
    db.flush()

    # Aging is meaningless until it is computed; a freshly uploaded batch must not sit at
    # days_past_due = 0 until the 00:30 job runs.
    refresh_aging(db, batch.merchant_id)
    return outcome


def _aging_snapshot(row: normalizer.NormalizedRow, *, is_msme: bool) -> dict[str, object]:
    """Aging fields for a newly created invoice.

    ``refresh_aging`` deliberately maintains only *open* invoices, so an invoice that arrives
    already paid would otherwise keep a NULL ``aging_bucket`` forever. Computing the snapshot at
    creation gives every invoice correct aging from the moment it exists, and open ones are then
    kept current by the nightly job.
    """
    assert row.due_date is not None  # guaranteed by normalize_row on the ok path
    days_past_due = (today() - row.due_date).days
    return {
        "days_past_due": days_past_due,
        "aging_bucket": bucket_of(days_past_due),
        "crosses_msme_45": is_msme and days_past_due > MSME_THRESHOLD_DAYS,
    }


def _status_for(amount_paise: int, outstanding_paise: int) -> str:
    """Derive payment status from the money. FR-3.4: partial payments track a residual."""
    if outstanding_paise <= 0:
        return PaymentStatus.PAID.value
    if outstanding_paise < amount_paise:
        return PaymentStatus.PARTIALLY_PAID.value
    return PaymentStatus.UNPAID.value


def _upsert_contact(db: Session, counterparty: Counterparty, row: normalizer.NormalizedRow) -> None:
    """Import contact details when the file carries them (FR-1.6).

    Matches on email, else phone, else name, so re-uploading the same file does not accumulate
    duplicate contacts for the same person.
    """
    if not (row.contact_email or row.contact_phone or row.contact_name):
        return

    contacts = list(
        db.execute(select(Contact).where(Contact.counterparty_id == counterparty.id)).scalars()
    )
    for contact in contacts:
        if row.contact_email and contact.email == row.contact_email:
            break
        if row.contact_phone and contact.phone == row.contact_phone:
            break
        if row.contact_name and contact.name == row.contact_name:
            break
    else:
        db.add(
            Contact(
                id=uuid.uuid4(),
                counterparty_id=counterparty.id,
                name=row.contact_name or row.contact_email or "(unnamed)",
                email=row.contact_email,
                phone=row.contact_phone,
                role=row.contact_role,
                is_primary=not contacts,
            )
        )
        return

    # Fill in details the existing contact was missing, without overwriting known-good values.
    contact.email = contact.email or row.contact_email
    contact.phone = contact.phone or row.contact_phone
    contact.role = contact.role or row.contact_role


def reprocess_row(
    db: Session,
    *,
    batch: Batch,
    batch_row: BatchRow,
    corrected: dict[str, str],
) -> tuple[BatchRow, str | None]:
    """Re-run one repaired row through normalisation and persistence.

    ``corrected`` is merged over the stored ``raw``, so the merchant only sends the cells they
    changed. Returns the row and an error detail when it still fails validation.
    """
    merged = {**batch_row.raw, **corrected}
    mapping = dict(batch.column_mapping)

    # A repaired row is re-read under the batch's own format where one was resolved. When the
    # whole batch failed as ambiguous there is no batch format, so the corrected row must be
    # self-describing (ISO or month-name) -- which is exactly what we asked the merchant for.
    decision = normalizer.detect_date_format(
        normalizer.date_values_for_detection([merged], mapping)
    )
    order = decision.order if decision.ok else normalizer.DateOrder.NOT_APPLICABLE

    row = normalizer.normalize_row(
        merged, row_number=batch_row.row_number, mapping=mapping, order=order
    )
    batch_row.raw = merged
    if not row.ok:
        batch_row.status = BatchRowStatus.REPAIR_NEEDED.value
        batch_row.error_code = row.error_code
        batch_row.error_detail = row.error_detail
        db.flush()
        return batch_row, row.error_detail

    try:
        match = matcher.resolve_counterparty(
            db, merchant_id=batch.merchant_id, name=row.counterparty_name, gstin=row.gstin
        )
    except matcher.AmbiguousMatchError as exc:
        batch_row.status = BatchRowStatus.REPAIR_NEEDED.value
        batch_row.error_code = RepairErrorCode.AMBIGUOUS_COUNTERPARTY.value
        batch_row.error_detail = str(exc)
        db.flush()
        return batch_row, str(exc)
    _upsert_contact(db, match.counterparty, row)

    invoice = db.execute(
        select(Invoice).where(
            Invoice.merchant_id == batch.merchant_id,
            Invoice.invoice_number == row.invoice_number,
        )
    ).scalar_one_or_none()

    if invoice is not None:
        invoice.outstanding_paise = row.outstanding_paise
        invoice.payment_status = _status_for(row.amount_paise, row.outstanding_paise)
    else:
        invoice = Invoice(
            id=uuid.uuid4(),
            merchant_id=batch.merchant_id,
            counterparty_id=match.counterparty.id,
            invoice_number=row.invoice_number,
            amount_paise=row.amount_paise,
            outstanding_paise=row.outstanding_paise,
            currency=row.currency,
            issue_date=row.issue_date,
            due_date=row.due_date,
            terms_days=row.terms_days,
            po_ref=row.po_ref,
            payment_status=_status_for(row.amount_paise, row.outstanding_paise),
            **_aging_snapshot(row, is_msme=match.counterparty.is_msme),
        )
        db.add(invoice)
        db.flush()
        batch.created_count += 1

    batch_row.status = BatchRowStatus.REPAIRED.value
    batch_row.error_code = None
    batch_row.error_detail = None
    batch_row.invoice_id = invoice.id
    batch.repair_count = max(0, batch.repair_count - 1)
    if batch.repair_count == 0:
        batch.status = BatchStatus.COMPLETE.value
    db.flush()

    refresh_aging(db, batch.merchant_id)
    return batch_row, None
