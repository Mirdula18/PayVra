"""invoices: is_pinned and snoozed_until for FR-4.5

POST /worklist/{id}/pin and /snooze need somewhere to put the merchant's decision.
/exclude does not: it is expressed with the existing recovery_state='stopped' plus
stop_reason='merchant_excluded', which is what those enum values are for.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-24
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "invoices",
        sa.Column(
            "is_pinned", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    # DATE, not TIMESTAMPTZ: a snooze is "not before this business day", and the business day is
    # the IST one (app.clock.today). Storing an instant would make the boundary timezone-sensitive.
    op.add_column("invoices", sa.Column("snoozed_until", sa.Date(), nullable=True))

    # Partial: only pinned rows are indexed, so it costs almost nothing and stays tiny.
    # GET /worklist fetches pins in a separate query (they cannot be ordered by idx_worklist
    # without defeating it for every other row). Without this, that query bitmap-scans every
    # active row for the merchant and filters -- measured at 5ms against a 200k-row book, and
    # scaling with book size rather than with the handful of rows actually pinned.
    op.create_index(
        "idx_worklist_pinned",
        "invoices",
        ["merchant_id"],
        unique=False,
        postgresql_where=sa.text("is_pinned"),
    )


def downgrade() -> None:
    # IF EXISTS: the index is partial and was added to this revision after an earlier
    # application of it, so a database stamped 0004 may legitimately predate the index.
    op.execute("DROP INDEX IF EXISTS idx_worklist_pinned")
    op.drop_column("invoices", "snoozed_until")
    op.drop_column("invoices", "is_pinned")
