"""``audit_log`` — append-only, hash-chained.

No UPDATE, no DELETE: enforced by two PostgreSQL RULEs created in the initial migration, not by
application convention. ``id`` is ``BIGSERIAL`` because ordering matters to the hash chain.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, enum_check, enum_column


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"), nullable=False)
    actor: Mapped[str] = enum_column()
    actor_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    # invoice | counterparty | action
    subject_type: Mapped[str] = mapped_column(Text, nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    inputs: Mapped[dict[str, object]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    gate_verdicts: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    # executed | blocked | stopped | approved | rejected
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    prev_hash: Mapped[str] = mapped_column(Text, nullable=False)
    entry_hash: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (enum_check("audit_log", "actor"),)
