"""Normalize integrity gap counters and preserve a non-null default.

Revision ID: 0006_integrity_counter_guard
Revises: 0005_vk_account_identity
"""

import sqlalchemy as sa

from alembic import op

revision = "0006_integrity_counter_guard"
down_revision = "0005_vk_account_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Normal production schema already has NOT NULL, but this UPDATE repairs any
    # drifted/manual schema before the constraint/default are asserted again.
    op.execute("UPDATE integrity_checks SET consecutive_gaps = 0 WHERE consecutive_gaps IS NULL")
    op.alter_column(
        "integrity_checks",
        "consecutive_gaps",
        existing_type=sa.Integer(),
        nullable=False,
        server_default=sa.text("0"),
    )


def downgrade() -> None:
    # Keep the data safe; only remove the explicit server default on downgrade.
    op.alter_column(
        "integrity_checks",
        "consecutive_gaps",
        existing_type=sa.Integer(),
        nullable=False,
        server_default=None,
    )
