"""``invoices`` — the core object. Money is BIGINT paise; the worklist index lives here."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    false,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Base,
    created_at_col,
    enum_check,
    enum_column,
    updated_at_col,
    uuid_pk,
)


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[uuid.UUID] = uuid_pk()
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"), nullable=False)
    counterparty_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("counterparties.id"), nullable=False
    )
    invoice_number: Mapped[str] = mapped_column(Text, nullable=False)

    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    outstanding_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), server_default=text("'INR'"), nullable=False)

    issue_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    terms_days: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    po_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    has_gst: Mapped[bool] = mapped_column(Boolean, server_default=false(), nullable=False)

    payment_status: Mapped[str] = enum_column(server_default=text("'unpaid'"))
    recovery_state: Mapped[str] = enum_column(server_default=text("'not_started'"))
    inferred_cause: Mapped[str] = enum_column(server_default=text("'unknown'"))

    days_past_due: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    aging_bucket: Mapped[str | None] = mapped_column(Text, nullable=True)
    crosses_msme_45: Mapped[bool] = mapped_column(Boolean, server_default=false(), nullable=False)

    collectability_score: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    priority_score: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    priority_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    touch_count: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"), nullable=False)
    current_tone_tier: Mapped[int] = mapped_column(
        SmallInteger, server_default=text("1"), nullable=False
    )
    stop_reason: Mapped[str | None] = enum_column(nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # FR-4.5 merchant overrides. Excluding is not here: it uses recovery_state='stopped' with
    # stop_reason='merchant_excluded', which the enums already carry.
    is_pinned: Mapped[bool] = mapped_column(Boolean, server_default=false(), nullable=False)
    snoozed_until: Mapped[date | None] = mapped_column(Date, nullable=True)

    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()

    __table_args__ = (
        enum_check("invoices", "payment_status"),
        enum_check("invoices", "recovery_state"),
        enum_check("invoices", "inferred_cause"),
        enum_check("invoices", "stop_reason"),
        Index("idx_invoice_number", "merchant_id", "invoice_number", unique=True),
        # Backs GET /worklist — the hot path.
        Index(
            "idx_worklist",
            "merchant_id",
            "recovery_state",
            text("priority_score DESC"),
        ),
        # Pinned rows are fetched separately from the ranked scan; partial so it indexes only
        # the handful of rows a merchant has actually pinned.
        Index("idx_worklist_pinned", "merchant_id", postgresql_where=text("is_pinned")),
    )
