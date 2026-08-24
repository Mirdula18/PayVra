"""``promises`` — promises to pay extracted from replies, and their outcome."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at_col, uuid_pk


class Promise(Base):
    __tablename__ = "promises"

    id: Mapped[uuid.UUID] = uuid_pk()
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id"), nullable=False)
    reply_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("replies.id"), nullable=False)
    promised_date: Mapped[date] = mapped_column(Date, nullable=False)
    # NULL means the full outstanding amount.
    promised_amount_paise: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    confidence: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)  # open | kept | broken | superseded
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = created_at_col()
