"""``merchants`` — tenant root. Every other table scopes to a merchant."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, SmallInteger, Text, false, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at_col, uuid_pk


class Merchant(Base):
    __tablename__ = "merchants"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)

    # Test-mode credentials; secrets are encrypted at rest (*_enc).
    razorpay_key_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    razorpay_key_secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    razorpay_webhook_secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)

    contact_hour_start: Mapped[int] = mapped_column(
        SmallInteger, server_default=text("8"), nullable=False
    )
    contact_hour_end: Mapped[int] = mapped_column(
        SmallInteger, server_default=text("19"), nullable=False
    )
    weekly_touch_cap: Mapped[int] = mapped_column(
        SmallInteger, server_default=text("2"), nullable=False
    )
    lifetime_touch_cap: Mapped[int] = mapped_column(
        SmallInteger, server_default=text("6"), nullable=False
    )
    approval_value_threshold_paise: Mapped[int] = mapped_column(
        BigInteger, server_default=text("50000000"), nullable=False
    )
    approval_tone_tier: Mapped[int] = mapped_column(
        SmallInteger, server_default=text("3"), nullable=False
    )
    is_paused: Mapped[bool] = mapped_column(Boolean, server_default=false(), nullable=False)

    created_at: Mapped[datetime] = created_at_col()
