"""``consents`` — the DPDP ledger: one row per counterparty per channel."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, enum_check, enum_column, uuid_pk


class Consent(Base):
    __tablename__ = "consents"

    id: Mapped[uuid.UUID] = uuid_pk()
    counterparty_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("counterparties.id"), nullable=False
    )
    channel: Mapped[str] = enum_column()
    is_permitted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    basis: Mapped[str] = mapped_column(Text, nullable=False)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Embedded in every message so a recipient can opt out.
    opt_out_token: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        enum_check("consents", "channel"),
        UniqueConstraint("counterparty_id", "channel", name="uq_consent_channel"),
    )
