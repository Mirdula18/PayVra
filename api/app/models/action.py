"""``actions`` — every proposal, gated or not, executed or not. The spine of the audit story."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, SmallInteger, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at_col, enum_check, enum_column, uuid_pk


class Action(Base):
    __tablename__ = "actions"

    id: Mapped[uuid.UUID] = uuid_pk()
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"), nullable=False)
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id"), nullable=False)

    type: Mapped[str] = enum_column()
    status: Mapped[str] = enum_column()
    channel: Mapped[str | None] = enum_column(nullable=True)
    tone_tier: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    proposed_by: Mapped[str] = enum_column()
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    llm_model: Mapped[str | None] = mapped_column(Text, nullable=True)

    # [{check, passed, reason}] — all 7 checks, always.
    gate_verdicts: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    gate_failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Circular FK with messages.action_id; emitted via ALTER after both tables exist.
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", use_alter=True, name="fk_actions_message_id"),
        nullable=True,
    )

    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = created_at_col()

    __table_args__ = (
        enum_check("actions", "type"),
        enum_check("actions", "status"),
        enum_check("actions", "channel"),
        enum_check("actions", "proposed_by"),
        # Backs the dispatch query (SELECT ... FOR UPDATE SKIP LOCKED).
        Index("idx_dispatch", "merchant_id", "status", "scheduled_for"),
        # Backs the revoke-on-settle sweep — the most important write path.
        Index("idx_actions_invoice", "invoice_id"),
    )
