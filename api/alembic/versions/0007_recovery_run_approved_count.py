"""recovery_runs.actions_approved — the outcome the run row was losing

A run's accounts end in one of three states, not two: a state change that *completed*
(``executed``), an outbound action the gate *approved* but which has no transport to deliver it
(``approved``), and a *refusal*. The row stored only executed and refused, so every approved action
vanished from the summary: a six-account run with one approval and five refusals rendered as
"0 done / 5 refused", and the sixth account was simply unaccounted for on screen.

Rolling approvals into ``actions_executed`` would have been the easy fix and the wrong one -- the
whole reason those are separate outcomes is that the audit log may never over-claim a send that
did not happen (see ``agent/runner.py``). So the third state gets its own column.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "recovery_runs",
        sa.Column("actions_approved", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("recovery_runs", "actions_approved")
