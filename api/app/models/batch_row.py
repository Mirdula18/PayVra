"""``batch_rows`` — the raw parsed row behind every ingested invoice and every repair-queue item.

Storing ``raw`` verbatim is what lets the repair queue show the merchant exactly what came out of
their file, and lets a corrected row be reprocessed without re-uploading. ``invoice_id`` links
back once the row successfully becomes an Invoice.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at_col, enum_check, enum_column, uuid_pk


class BatchRow(Base):
    __tablename__ = "batch_rows"

    id: Mapped[uuid.UUID] = uuid_pk()
    batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("batches.id"), nullable=False)
    # 1-based, matching what the merchant sees in their spreadsheet (header is row 1, so the
    # first data row is 2). Stored so the repair queue can say "row 47 of your file".
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)

    raw: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )

    status: Mapped[str] = enum_column(server_default=text("'ok'"))
    # Plain TEXT, not a CHECK-constrained enum -- see RepairErrorCode in app.enums for why.
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    invoice_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("invoices.id"), nullable=True)

    created_at: Mapped[datetime] = created_at_col()

    __table_args__ = (
        enum_check("batch_rows", "status"),
        # Backs GET /batches/{id}/repairs, which filters to status='repair_needed'.
        Index("idx_batch_rows_status", "batch_id", "status"),
    )
