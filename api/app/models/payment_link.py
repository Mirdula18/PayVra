"""``payment_links`` — Razorpay Payment Links. ``reference_id`` is the reconciliation key."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at_col, uuid_pk


class PaymentLink(Base):
    __tablename__ = "payment_links"

    id: Mapped[uuid.UUID] = uuid_pk()
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id"), nullable=False)
    razorpay_link_id: Mapped[str] = mapped_column(Text, nullable=False)
    short_url: Mapped[str] = mapped_column(Text, nullable=False)
    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Equals invoice_number — the key reconciliation matches a webhook payment to an invoice on.
    reference_id: Mapped[str] = mapped_column(Text, nullable=False)
    # created | paid | partially_paid | expired | cancelled (Razorpay's own vocabulary).
    status: Mapped[str] = mapped_column(Text, nullable=False)
    expire_by: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accept_partial: Mapped[bool] = mapped_column(Boolean, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = created_at_col()

    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_payment_link_idempotency"),)
