"""Pydantic models for the worklist endpoints (architecture/api-contracts.md -> Worklist)."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field


class CounterpartyRef(BaseModel):
    id: uuid.UUID
    name: str


class WorklistItem(BaseModel):
    """One ranked row. ``priority_reason`` is required -- FR-4.3, never null."""

    invoice_id: uuid.UUID
    invoice_number: str
    counterparty: CounterpartyRef
    outstanding_paise: int
    days_past_due: int
    aging_bucket: str | None
    crosses_msme_45: bool
    recovery_state: str
    inferred_cause: str
    collectability_score: Decimal
    priority_score: Decimal
    priority_reason: str
    current_tone_tier: int
    touch_count: int
    is_pinned: bool = False
    snoozed_until: date | None = None
    # The contract carries a proposed_action object. The agent that proposes actions is Phase 6;
    # until it exists this is null rather than a fabricated placeholder, so the frontend can build
    # against the real shape and a judge is never shown an action nothing actually planned.
    proposed_action: None = None


class WorklistSummary(BaseModel):
    total_outstanding_paise: int
    overdue_count: int
    high_risk_count: int


class WorklistResponse(BaseModel):
    items: list[WorklistItem]
    total: int
    summary: WorklistSummary
    limit: int
    offset: int


class SnoozeRequest(BaseModel):
    until: date = Field(description="Business date (IST) the invoice returns to the worklist on.")


class WorklistActionResult(BaseModel):
    invoice_id: uuid.UUID
    is_pinned: bool
    snoozed_until: date | None
    recovery_state: str
    stop_reason: str | None
