"""recovery_runs, actions.recovery_run_id, and a run filter on audit_log

Phase 6 (ADR-009). One batch-runner pass is a ``recovery_run``, and that id is the scope every
recovery figure is measured in.

**audit_log deliberately gets no new column.** Its tamper-evidence comes from a hash over a
canonical serialisation of the row's business fields, and ``inputs`` is inside that hash while a
newly added column would not be. A run id stored in an unhashed column could be altered without
breaking the chain -- which is exactly the guarantee the audit log exists to provide, on exactly
the attribute a judge reads. So the runner writes ``inputs->>'recovery_run_id'`` instead, and this
migration adds an expression index so filtering the trail to one run stays an indexed lookup.
That deviates from ADR-009's schema table by design; the reasoning is recorded here.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.enums import RecoveryRunStatus, check_expression

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recovery_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("account_limit", sa.Integer(), nullable=False),
        sa.Column("dry_run", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("contact_hour_start", sa.SmallInteger(), nullable=False),
        sa.Column("contact_hour_end", sa.SmallInteger(), nullable=False),
        sa.Column("window_overridden", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("accounts_considered", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("actions_proposed", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("actions_executed", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("actions_refused", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["merchant_id"], ["merchants.id"]),
        sa.CheckConstraint(
            check_expression("status", RecoveryRunStatus), name="ck_recovery_runs_status"
        ),
    )
    op.create_index(
        "idx_recovery_runs_merchant", "recovery_runs", ["merchant_id", "started_at"]
    )

    op.add_column("actions", sa.Column("recovery_run_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_actions_recovery_run_id", "actions", "recovery_runs", ["recovery_run_id"], ["id"]
    )
    op.create_index("idx_actions_recovery_run", "actions", ["recovery_run_id"])

    # Filtering the audit trail to one run is the clause 3 and 4 demo, so it must not be a scan.
    op.execute(
        "CREATE INDEX idx_audit_recovery_run ON audit_log ((inputs->>'recovery_run_id'))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_audit_recovery_run")
    op.drop_index("idx_actions_recovery_run", table_name="actions")
    op.drop_constraint("fk_actions_recovery_run_id", "actions", type_="foreignkey")
    op.drop_column("actions", "recovery_run_id")
    op.drop_index("idx_recovery_runs_merchant", table_name="recovery_runs")
    op.drop_table("recovery_runs")
