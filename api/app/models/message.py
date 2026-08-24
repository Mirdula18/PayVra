"""``messages`` — the rendered outreach text and its delivery/engagement state."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, SmallInteger, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at_col, enum_check, enum_column, uuid_pk


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = uuid_pk()
    action_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("actions.id"), nullable=False)
    channel: Mapped[str] = enum_column()
    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contacts.id"), nullable=False)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)  # email only
    body: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(Text, nullable=False)  # en | hinglish
    tone_tier: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)  # llm | template
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    validation_passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # queued | sent | delivered | bounced | failed
    delivery_status: Mapped[str] = mapped_column(Text, nullable=False)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    clicked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = created_at_col()

    __table_args__ = (enum_check("messages", "channel"),)
