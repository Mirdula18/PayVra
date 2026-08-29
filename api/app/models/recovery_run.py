"""``recovery_runs`` — one pass of the batch runner. The scope every recovery figure is measured in.

**Not the same thing as ``batches``.** A batch is an uploaded invoice *file*; a recovery run is one
execution of the agent over a ranked worklist. A file is imported once, a worklist is run against
repeatedly. Sharing an id column between them would give one word two meanings in the place it
hurts most — a judge asking what a number is scoped to. See ADR-009.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, SmallInteger, false, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at_col, enum_check, enum_column, uuid_pk


class RecoveryRun(Base):
    __tablename__ = "recovery_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"), nullable=False)

    status: Mapped[str] = enum_column()
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # How many worklist rows this run was allowed to touch. Recorded rather than inferred: the
    # limit that produced a figure is part of reading that figure honestly.
    account_limit: Mapped[int] = mapped_column(Integer, nullable=False)

    # A dry run gates and records verdicts but creates no link and sends nothing (FR-16.7).
    dry_run: Mapped[bool] = mapped_column(Boolean, server_default=false(), nullable=False)

    # The contact window actually in force, always. When an override widened it, these differ from
    # the merchant's configured hours and the run is compliant *by record* rather than by
    # assertion (FR-16.8) -- a reader can see the window was widened without taking anyone's word.
    contact_hour_start: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    contact_hour_end: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    window_overridden: Mapped[bool] = mapped_column(
        Boolean, server_default=false(), nullable=False
    )

    accounts_considered: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), nullable=False
    )
    actions_proposed: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), nullable=False
    )
    actions_executed: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), nullable=False
    )
    actions_refused: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)

    created_at: Mapped[datetime] = created_at_col()

    __table_args__ = (
        enum_check("recovery_runs", "status"),
        Index("idx_recovery_runs_merchant", "merchant_id", "started_at"),
    )
