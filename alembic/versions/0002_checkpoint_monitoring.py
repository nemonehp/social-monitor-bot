"""Checkpoint monitoring without historical bootstrap.

Revision ID: 0002_checkpoint_monitoring
Revises: 0001_initial

This migration intentionally clears collection history produced by the old
bootstrap implementation. Sources, credentials, proxies, users and application
settings are preserved. The migration establishes "now" as the monitoring
boundary for every existing source.
"""
import sqlalchemy as sa

from alembic import op

revision = "0002_checkpoint_monitoring"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "source_states",
        sa.Column(
            "monitor_from_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.add_column(
        "source_states",
        sa.Column(
            "checkpoint_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    # Be defensive: every source should have a state row, but an interrupted
    # older import may have left one missing. Such rows also start monitoring now.
    op.execute(
        """
        INSERT INTO source_states (
            source_id, monitor_from_at, checkpoint_at, bootstrap_completed,
            post_watermark, story_watermark, post_cursor, story_cursor,
            recent_post_keys, recent_story_keys, created_at, updated_at
        )
        SELECT s.id, now(), now(), false, '', '', '{}'::jsonb, '{}'::jsonb,
               '[]'::jsonb, '[]'::jsonb, now(), now()
        FROM sources AS s
        WHERE NOT EXISTS (
            SELECT 1 FROM source_states AS state WHERE state.source_id = s.id
        )
        """
    )

    # Old versions imported historical Telegram posts/stories during bootstrap.
    # They were never delivered, but keeping them would make the new boundary
    # ambiguous. Cascades remove media/deliveries linked to deleted items.
    op.execute("DELETE FROM collection_jobs")
    op.execute("DELETE FROM deliveries")
    op.execute("DELETE FROM items")
    op.execute(
        """
        UPDATE source_states
        SET monitor_from_at = now(),
            checkpoint_at = now(),
            bootstrap_completed = false,
            post_watermark = '',
            story_watermark = '',
            post_cursor = '{}'::jsonb,
            story_cursor = '{}'::jsonb,
            recent_post_keys = '[]'::jsonb,
            recent_story_keys = '[]'::jsonb
        """
    )
    op.execute(
        """
        UPDATE sources
        SET next_check_at = now(),
            last_check_at = NULL,
            last_success_at = NULL,
            last_item_at = NULL,
            consecutive_failures = 0,
            last_error_code = '',
            last_error_text = ''
        WHERE status::text <> 'DELETED'
        """
    )


def downgrade() -> None:
    op.drop_column("source_states", "checkpoint_at")
    op.drop_column("source_states", "monitor_from_at")
