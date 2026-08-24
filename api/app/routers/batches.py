"""Ingestion endpoints (architecture/api-contracts.md -> Ingestion).

Every handler takes ``merchant_id`` from :func:`app.deps.current_merchant_id` and scopes every
query with it. No handler accepts a merchant id from the caller. ``_load_batch`` is the single
place a batch is fetched, and it filters on merchant, so a cross-tenant batch id is a 404 rather
than someone else's data.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Form, Query, UploadFile, status
from sqlalchemy import func, select

from app.deps import DbSession, MerchantId
from app.enums import BatchRowStatus, BatchStatus
from app.exceptions import IngestionError, NotFoundError, ValidationError
from app.ingestion import mapper, pipeline
from app.ingestion.parsers import parse_upload
from app.models.batch import Batch
from app.models.batch_row import BatchRow
from app.schemas.batch import (
    BatchIngestResponse,
    MappingOverride,
    RepairListResponse,
    RepairResult,
    RepairRow,
    RepairSubmission,
)

router = APIRouter(prefix="/batches", tags=["ingestion"])

# 25 MB. A 50k-row invoice export is a few MB; anything larger is a mistake or an attack.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def _load_batch(db: DbSession, merchant_id: uuid.UUID, batch_id: uuid.UUID) -> Batch:
    """Fetch a batch scoped to the caller's merchant. Cross-tenant ids are indistinguishable
    from missing ones -- see :class:`app.exceptions.NotFoundError`."""
    batch = db.execute(
        select(Batch).where(Batch.id == batch_id, Batch.merchant_id == merchant_id)
    ).scalar_one_or_none()
    if batch is None:
        raise NotFoundError(f"batch {batch_id} not found")
    return batch


def _response(batch: Batch, outcome: pipeline.IngestOutcome) -> BatchIngestResponse:
    return BatchIngestResponse(
        batch_id=batch.id,
        created=outcome.created,
        updated=outcome.updated,
        duplicates=outcome.duplicates,
        repair_queue=outcome.repairs,
        counterparties_matched=outcome.counterparties_matched,
        counterparties_quarantined=outcome.counterparties_quarantined,
        column_mapping=dict(batch.column_mapping),
        total_outstanding_paise=outcome.total_outstanding_paise,
        unmapped_headers=outcome.unmapped_headers,
        status=batch.status,
    )


@router.post("", response_model=BatchIngestResponse, status_code=status.HTTP_201_CREATED)
def create_batch(
    db: DbSession,
    merchant_id: MerchantId,
    file: Annotated[UploadFile, File()],
    name: Annotated[str | None, Form()] = None,
) -> BatchIngestResponse:
    """Upload a CSV or XLSX of invoices and ingest it.

    Runs synchronously: parsing and normalisation are pure CPU over a bounded file, and there is
    no LLM call on this path (agents/backend.md ground rule 3), so there is nothing to enqueue.
    """
    data = file.file.read()
    if not data:
        raise ValidationError("uploaded file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValidationError(f"file is {len(data)} bytes; the limit is {MAX_UPLOAD_BYTES} bytes")

    filename = file.filename or "upload.csv"
    parsed = parse_upload(filename, data)  # raises IngestionError -> 422

    batch = Batch(
        id=uuid.uuid4(),
        merchant_id=merchant_id,
        name=name,
        filename=filename,
        status=BatchStatus.PARSING.value,
    )
    db.add(batch)
    db.flush()

    mapping_result = mapper.map_headers(parsed.headers)
    outcome = pipeline.ingest_rows(db, batch=batch, parsed=parsed, mapping=mapping_result.mapping)
    # Persisted even when the batch failed wholesale, so the merchant can see what we did detect
    # and correct it via POST /batches/{id}/mapping.
    batch.column_mapping = dict(mapping_result.mapping)
    db.commit()
    return _response(batch, outcome)


@router.get("/{batch_id}/repairs", response_model=RepairListResponse)
def list_repairs(
    db: DbSession,
    merchant_id: MerchantId,
    batch_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> RepairListResponse:
    """Rows that failed validation, with the reason. Paginated (backend.md: anything over 100)."""
    batch = _load_batch(db, merchant_id, batch_id)

    condition = (
        BatchRow.batch_id == batch.id,
        BatchRow.status == BatchRowStatus.REPAIR_NEEDED.value,
    )
    total = db.execute(select(func.count()).select_from(BatchRow).where(*condition)).scalar_one()
    rows = (
        db.execute(
            select(BatchRow)
            .where(*condition)
            .order_by(BatchRow.row_number)
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    return RepairListResponse(
        batch_id=batch.id,
        items=[
            RepairRow(
                row_id=row.id,
                row_number=row.row_number,
                raw=row.raw,
                error_code=row.error_code,
                error_detail=row.error_detail,
                status=row.status,
                created_at=row.created_at,
            )
            for row in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/{batch_id}/repairs/{row_id}", response_model=RepairResult)
def submit_repair(
    db: DbSession,
    merchant_id: MerchantId,
    batch_id: uuid.UUID,
    row_id: uuid.UUID,
    body: RepairSubmission,
) -> RepairResult:
    """Submit corrected values for one row, or discard it.

    The corrected row is re-run through the same normalisation as the original upload -- a repair
    is never trusted into the database unvalidated. A row that still fails comes back with the
    next error rather than being accepted.
    """
    batch = _load_batch(db, merchant_id, batch_id)
    row = db.execute(
        select(BatchRow).where(BatchRow.id == row_id, BatchRow.batch_id == batch.id)
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"row {row_id} not found in batch {batch_id}")

    if body.discard:
        if row.status == BatchRowStatus.REPAIR_NEEDED.value:
            batch.repair_count = max(0, batch.repair_count - 1)
        row.status = BatchRowStatus.DISCARDED.value
        row.error_code = None
        row.error_detail = None
        if batch.repair_count == 0:
            batch.status = BatchStatus.COMPLETE.value
        db.commit()
        return RepairResult(
            row_id=row.id,
            status=row.status,
            remaining_repairs=batch.repair_count,
            batch_status=batch.status,
        )

    if not body.values:
        raise ValidationError("provide corrected 'values', or set 'discard': true")

    row, error_detail = pipeline.reprocess_row(
        db, batch=batch, batch_row=row, corrected=body.values
    )
    db.commit()
    return RepairResult(
        row_id=row.id,
        status=row.status,
        invoice_id=row.invoice_id,
        error_code=row.error_code,
        error_detail=error_detail,
        remaining_repairs=batch.repair_count,
        batch_status=batch.status,
    )


@router.post("/{batch_id}/mapping", response_model=BatchIngestResponse)
def override_mapping(
    db: DbSession,
    merchant_id: MerchantId,
    batch_id: uuid.UUID,
    body: MappingOverride,
) -> BatchIngestResponse:
    """Override the auto-detected column mapping and re-parse the batch.

    Re-parsing needs the original file, which we do not store (invoice files are merchant PII and
    keeping them is a liability we do not need). Instead the batch's own ``batch_rows`` hold every
    raw row verbatim -- that is exactly what they are for -- so the re-parse runs off them and the
    merchant never re-uploads.
    """
    batch = _load_batch(db, merchant_id, batch_id)

    stored = (
        db.execute(
            select(BatchRow).where(BatchRow.batch_id == batch.id).order_by(BatchRow.row_number)
        )
        .scalars()
        .all()
    )
    if not stored:
        raise NotFoundError(f"batch {batch_id} has no stored rows to re-parse")

    headers = list(stored[0].raw.keys())
    try:
        mapping_result = mapper.apply_override(headers, body.column_mapping)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc

    from app.ingestion.parsers import ParsedFile

    parsed = ParsedFile(headers=headers, rows=[dict(row.raw) for row in stored])

    # Drop the previous attempt's rows; this batch is being re-decided from scratch. The invoices
    # already created are left alone -- a re-parse is a mapping correction, not a rollback, and
    # re-ingesting hits the duplicate path which never resets recovery state.
    for row in stored:
        db.delete(row)
    db.flush()

    outcome = pipeline.ingest_rows(db, batch=batch, parsed=parsed, mapping=mapping_result.mapping)
    db.commit()
    return _response(batch, outcome)


__all__ = ["router", "IngestionError"]
