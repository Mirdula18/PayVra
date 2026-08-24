"""``batches`` — one uploaded invoice file and its ingestion outcome.

Holds the resolved ``column_mapping`` so a re-parse (POST /batches/{id}/mapping) does not need
the merchant to re-upload, and the per-outcome counters that POST /batches returns.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at_col, enum_check, enum_column, uuid_pk


class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[uuid.UUID] = uuid_pk()
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"), nullable=False)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    filename: Mapped[str] = mapped_column(Text, nullable=False)

    # Header -> canonical field, as resolved by ingestion.mapper (or overridden by the merchant).
    column_mapping: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )

    row_count: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    created_count: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    updated_count: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    duplicate_count: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    repair_count: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)

    status: Mapped[str] = enum_column(server_default=text("'parsing'"))

    created_at: Mapped[datetime] = created_at_col()

    __table_args__ = (enum_check("batches", "status"),)
