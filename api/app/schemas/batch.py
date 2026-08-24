"""Pydantic models for the ingestion endpoints (architecture/api-contracts.md).

Field names and shapes follow the contract exactly -- the frontend is built against it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class BatchIngestResponse(BaseModel):
    """Response to POST /batches."""

    batch_id: uuid.UUID
    created: int
    updated: int
    duplicates: int
    repair_queue: int
    counterparties_matched: int
    counterparties_quarantined: int
    column_mapping: dict[str, str]
    total_outstanding_paise: int
    # Not in the original contract, but a merchant whose header we could not place needs to know
    # which one before they can call POST /batches/{id}/mapping.
    unmapped_headers: list[str] = Field(default_factory=list)
    status: str


class RepairRow(BaseModel):
    """One row in the repair queue."""

    row_id: uuid.UUID
    row_number: int
    raw: dict[str, Any]
    error_code: str | None
    error_detail: str | None
    status: str
    created_at: datetime


class RepairListResponse(BaseModel):
    """Response to GET /batches/{batch_id}/repairs."""

    batch_id: uuid.UUID
    items: list[RepairRow]
    total: int
    limit: int
    offset: int


class RepairSubmission(BaseModel):
    """Body of POST /batches/{batch_id}/repairs/{row_id}.

    ``values`` is merged over the stored raw row, so the merchant sends only the cells they
    changed, keyed by the file's own header names.
    """

    values: dict[str, str] = Field(default_factory=dict)
    discard: bool = False


class RepairResult(BaseModel):
    row_id: uuid.UUID
    status: str
    invoice_id: uuid.UUID | None = None
    error_code: str | None = None
    error_detail: str | None = None
    remaining_repairs: int
    batch_status: str


class MappingOverride(BaseModel):
    """Body of POST /batches/{batch_id}/mapping: original header -> canonical field."""

    column_mapping: dict[str, str]


class ExposureItem(BaseModel):
    counterparty_id: uuid.UUID
    name: str
    is_msme: bool
    open_invoice_count: int
    outstanding_paise: int
    overdue_paise: int
    max_days_past_due: int
    msme_breach_count: int
    concentration: float
