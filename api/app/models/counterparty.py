"""``counterparties`` — the customers being chased, with fuzzy-match and consent flags."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at_col, uuid_pk


class Counterparty(Base):
    __tablename__ = "counterparties"

    id: Mapped[uuid.UUID] = uuid_pk()
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # Lowercased, suffixes stripped, for fuzzy matching.
    name_normalized: Mapped[str] = mapped_column(Text, nullable=False)
    gstin: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_msme: Mapped[bool] = mapped_column(Boolean, server_default=false(), nullable=False)
    preferred_language: Mapped[str] = mapped_column(
        Text, server_default=text("'en'"), nullable=False
    )
    lifetime_revenue_paise: Mapped[int] = mapped_column(
        BigInteger, server_default=text("0"), nullable=False
    )
    avg_days_to_pay: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    broken_promise_count: Mapped[int] = mapped_column(
        SmallInteger, server_default=text("0"), nullable=False
    )
    is_quarantined: Mapped[bool] = mapped_column(Boolean, server_default=false(), nullable=False)
    is_excluded: Mapped[bool] = mapped_column(Boolean, server_default=false(), nullable=False)

    created_at: Mapped[datetime] = created_at_col()

    __table_args__ = (
        # Unique GSTIN per merchant, but only when present (many rows have no GSTIN).
        UniqueConstraint("merchant_id", "gstin", name="uq_counterparty_gstin"),
        Index("idx_cp_match", "merchant_id", "name_normalized"),
    )
