"""payment_links: razorpay_link_id must be unique

Reconciliation resolves a webhook to an invoice by looking up the Razorpay link id, and does so
with `scalar_one_or_none()` — it assumes at most one row. Nothing enforced that. A Razorpay link
id *is* globally unique, so two rows carrying the same one is a data-integrity fault, and the
right place to catch it is the constraint rather than a MultipleResultsFound at reconciliation
time on a payment that has already been taken.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-24
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "idx_payment_link_razorpay_id", "payment_links", ["razorpay_link_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index("idx_payment_link_razorpay_id", table_name="payment_links")
