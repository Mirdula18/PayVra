"""``replies`` — inbound messages from counterparties, classified by intent."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Numeric, Text, false
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, enum_check, enum_column, uuid_pk


class Reply(Base):
    __tablename__ = "replies"

    id: Mapped[uuid.UUID] = uuid_pk()
    # May arrive unlinked to an invoice.
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("invoices.id"), nullable=True)
    counterparty_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("counterparties.id"), nullable=False
    )
    channel: Mapped[str] = enum_column()
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[str] = enum_column()
    confidence: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    extracted_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # True when confidence < REPLY_CONFIDENCE_THRESHOLD.
    routed_to_human: Mapped[bool] = mapped_column(Boolean, server_default=false(), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        enum_check("replies", "channel"),
        enum_check("replies", "intent"),
    )
