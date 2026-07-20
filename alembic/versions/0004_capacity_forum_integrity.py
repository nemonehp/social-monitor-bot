"""Capacity guard, VK proxy affinity and integrity control.

Revision ID: 0004_capacity_forum_integrity
Revises: 0003_unified_health_categories
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0004_capacity_forum_integrity"
down_revision = "0003_unified_health_categories"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("credentials", sa.Column("assigned_proxy_id", sa.BigInteger(), nullable=True))
    op.add_column(
        "credentials",
        sa.Column("assigned_external_ip", sa.String(length=100), server_default="", nullable=False),
    )
    op.add_column(
        "credentials", sa.Column("assignment_epoch", sa.String(length=64), server_default="", nullable=False)
    )
    op.add_column(
        "credentials", sa.Column("rate_limit_events", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column("credentials", sa.Column("last_rate_limit_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("credentials", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        "fk_credentials_assigned_proxy",
        "credentials",
        "proxies",
        ["assigned_proxy_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_credentials_assigned_proxy", "credentials", ["assigned_proxy_id"])

    op.create_table(
        "api_usage",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("credential_id", sa.BigInteger(), nullable=False),
        sa.Column("platform", sa.String(length=20), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("request_count", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("source_checks", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("rate_limit_events", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["credential_id"], ["credentials.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("credential_id", "usage_date", name="uq_api_usage_credential_date"),
    )
    op.create_index("ix_api_usage_platform_date", "api_usage", ["platform", "usage_date"])

    op.create_table(
        "integrity_checks",
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_remote_post_id", sa.String(length=255), server_default="", nullable=False),
        sa.Column("last_remote_story_id", sa.String(length=255), server_default="", nullable=False),
        sa.Column("last_stored_post_id", sa.String(length=255), server_default="", nullable=False),
        sa.Column("last_stored_story_id", sa.String(length=255), server_default="", nullable=False),
        sa.Column("status", sa.String(length=50), server_default="unknown", nullable=False),
        sa.Column("consecutive_gaps", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "details_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("source_id"),
    )


def downgrade() -> None:
    op.drop_table("integrity_checks")
    op.drop_index("ix_api_usage_platform_date", table_name="api_usage")
    op.drop_table("api_usage")
    op.drop_index("ix_credentials_assigned_proxy", table_name="credentials")
    op.drop_constraint("fk_credentials_assigned_proxy", "credentials", type_="foreignkey")
    op.drop_column("credentials", "expires_at")
    op.drop_column("credentials", "last_rate_limit_at")
    op.drop_column("credentials", "rate_limit_events")
    op.drop_column("credentials", "assignment_epoch")
    op.drop_column("credentials", "assigned_external_ip")
    op.drop_column("credentials", "assigned_proxy_id")
