"""audit_log: forbid TRUNCATE, add the GET /audit query indexes

Migration 0001 made audit_log append-only with two RULEs (no UPDATE, no DELETE). RULEs do not
cover TRUNCATE, so `TRUNCATE audit_log CASCADE` silently emptied the table -- verified against a
live database during Phase 0 verification. This closes that hole with a BEFORE TRUNCATE trigger.

The trigger raises unconditionally. There is deliberately no GUC check and no session-variable
bypass: "append-only except when a flag is set" is not a guarantee anyone should rely on. The
seed rebuilds the schema (alembic downgrade base && alembic upgrade head) instead of truncating.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-24
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_log_forbid_truncate() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only: TRUNCATE is not permitted'
                USING ERRCODE = '42501',
                      HINT = 'Rebuild the schema instead: alembic downgrade base && alembic upgrade head';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    # FOR EACH STATEMENT is the only level a TRUNCATE trigger can fire at. It also fires when
    # audit_log is reached indirectly, via TRUNCATE ... CASCADE from merchants.
    op.execute(
        """
        CREATE TRIGGER audit_log_no_truncate
        BEFORE TRUNCATE ON audit_log
        FOR EACH STATEMENT
        EXECUTE FUNCTION audit_log_forbid_truncate();
        """
    )

    # Backs GET /audit (api-contracts.md): the default reverse-chronological merchant feed, and
    # the AuditLog screen's one-click "blocked" filter.
    op.create_index(
        "idx_audit_recent",
        "audit_log",
        ["merchant_id", sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_index("idx_audit_outcome", "audit_log", ["merchant_id", "outcome"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_audit_outcome", table_name="audit_log")
    op.drop_index("idx_audit_recent", table_name="audit_log")
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_forbid_truncate()")
