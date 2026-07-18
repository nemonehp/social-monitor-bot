"""Unified delivery, credential health and source categories.

Revision ID: 0003_unified_delivery_health_categories
Revises: 0002_checkpoint_monitoring
"""

import sqlalchemy as sa

from alembic import op

revision = "0003_unified_delivery_health_categories"
down_revision = "0002_checkpoint_monitoring"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Native PostgreSQL enums store Python enum member names.
    op.execute("ALTER TYPE credential_status_enum ADD VALUE IF NOT EXISTS 'LIMITED'")

    op.add_column(
        "sources",
        sa.Column("category", sa.String(length=255), server_default="", nullable=False),
    )
    op.add_column(
        "sources",
        sa.Column("subcategory", sa.String(length=255), server_default="", nullable=False),
    )
    op.create_index("ix_sources_category", "sources", ["category", "subcategory"])
    op.execute(
        """
        UPDATE sources
        SET category = COALESCE(NULLIF(federal_district, ''), ''),
            subcategory = COALESCE(NULLIF(region, ''), '')
        """
    )

    op.add_column(
        "credentials",
        sa.Column("last_health_check_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "credentials",
        sa.Column("last_health_ok_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "credentials",
        sa.Column("health_failures", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "credentials",
        sa.Column("dead_since", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "credentials",
        sa.Column("dead_notified_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column(
        "proxies",
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        """
        UPDATE proxies
        SET last_success_at = CASE
            WHEN successes > 0 THEN COALESCE(last_check_at, created_at)
            ELSE NULL
        END
        """
    )


def downgrade() -> None:
    op.drop_column("proxies", "last_success_at")
    op.drop_column("credentials", "dead_notified_at")
    op.drop_column("credentials", "dead_since")
    op.drop_column("credentials", "health_failures")
    op.drop_column("credentials", "last_health_ok_at")
    op.drop_column("credentials", "last_health_check_at")
    op.drop_index("ix_sources_category", table_name="sources")
    op.drop_column("sources", "subcategory")
    op.drop_column("sources", "category")
    # PostgreSQL enum values are intentionally not removed on downgrade.
