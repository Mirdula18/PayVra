"""Declarative base and shared schema helpers."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import String

from app.enums import ENUM_COLUMNS, check_expression

# Width for VARCHAR enum columns. Longest enum value is 'broken_promises_exceeded' (24);
# 32 leaves headroom for values added in later phases.
ENUM_LEN = 32


class Base(DeclarativeBase):
    """SQLAlchemy 2.0 declarative base for every PAYVRA table."""


def enum_check(table: str, column: str) -> CheckConstraint:
    """Build the ``CHECK (col IN (...))`` constraint for an enum column from ENUM_COLUMNS.

    Generating from the single source (app.enums) guarantees the constraint and the StrEnum
    cannot drift; test_enum_parity verifies coverage.
    """
    enum_cls = ENUM_COLUMNS[(table, column)]
    return CheckConstraint(check_expression(column, enum_cls), name=f"ck_{table}_{column}")


def enum_column(nullable: bool = False, **kwargs: object) -> Mapped[str]:
    """A VARCHAR column holding an enum value (constraint is added via ``enum_check``)."""
    return mapped_column(String(ENUM_LEN), nullable=nullable, **kwargs)  # type: ignore[arg-type]


def uuid_pk() -> Mapped[uuid.UUID]:
    """UUID primary key with a client-side default."""
    return mapped_column(primary_key=True, default=uuid.uuid4)


def created_at_col() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def updated_at_col() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
