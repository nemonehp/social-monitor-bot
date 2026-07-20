"""Canonical VK account identity.

Revision ID: 0005_vk_account_identity
Revises: 0004_capacity_forum_integrity
"""

import sqlalchemy as sa

from alembic import op

revision = "0005_vk_account_identity"
down_revision = "0004_capacity_forum_integrity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "credentials",
        sa.Column("external_account_id", sa.String(length=100), nullable=True),
    )

    # Backfill only identities that are unambiguous. Duplicate legacy rows are
    # intentionally left NULL and are consolidated after a live token proves
    # the actual VK user through users.get.
    op.execute(
        """
        WITH known AS (
            SELECT
                config_json ->> 'user_id' AS account_id,
                count(*) AS copies
            FROM credentials
            WHERE platform::text = 'VK'
              AND config_json ? 'user_id'
              AND (config_json ->> 'user_id') ~ '^[0-9]+$'
            GROUP BY config_json ->> 'user_id'
        )
        UPDATE credentials AS credential
        SET external_account_id = credential.config_json ->> 'user_id'
        FROM known
        WHERE credential.platform::text = 'VK'
          AND credential.config_json ->> 'user_id' = known.account_id
          AND known.copies = 1
        """
    )
    # Canonicalize labels for identities that were already unambiguous. A
    # legacy ordinal label is moved away first if it happens to occupy the
    # desired `vk-<ID>` value of another credential.
    op.execute(
        """
        UPDATE credentials AS legacy
        SET label = 'vk-legacy-' || legacy.id::text
        WHERE legacy.platform::text = 'VK'
          AND EXISTS (
              SELECT 1
              FROM credentials AS known
              WHERE known.platform::text = 'VK'
                AND known.external_account_id IS NOT NULL
                AND known.id <> legacy.id
                AND legacy.label = 'vk-' || known.external_account_id
          )
        """
    )
    op.execute(
        """
        UPDATE credentials
        SET label = 'vk-' || external_account_id
        WHERE platform::text = 'VK'
          AND external_account_id IS NOT NULL
        """
    )

    op.create_unique_constraint(
        "uq_credentials_platform_external_account",
        "credentials",
        ["platform", "external_account_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_credentials_platform_external_account",
        "credentials",
        type_="unique",
    )
    op.drop_column("credentials", "external_account_id")
