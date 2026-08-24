"""``metrics_snapshots`` — daily rollup so the dashboard never recomputes DSO on the fly."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import BigInteger, Date, ForeignKey, Numeric, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, uuid_pk


class MetricsSnapshot(Base):
    __tablename__ = "metrics_snapshots"

    id: Mapped[uuid.UUID] = uuid_pk()
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"), nullable=False)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    total_outstanding_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    recovered_paise: Mapped[int] = mapped_column(
        BigInteger, nullable=False
    )  # since previous snapshot
    dso_days: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    recovery_rate: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    promise_kept_rate: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    invoices_by_state: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        UniqueConstraint("merchant_id", "snapshot_date", name="uq_metrics_snapshot_date"),
    )
