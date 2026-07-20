"""Reset false-positive integrity gaps after observed-ID tracking.

Revision ID: 0007_integrity_observation
Revises: 0006_integrity_counter_guard
"""

from alembic import op

revision = "0007_integrity_observation"
down_revision = "0006_integrity_counter_guard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # v1.3.0 compared the remote latest ID only with stored items. Service
    # messages, historical baseline posts and intentionally skipped pinned posts
    # were therefore reported as permanent gaps even though collectors had seen
    # them. Clear those accumulated counters; the next collection pass rebuilds
    # status using explicit observed IDs.
    op.execute(
        """
        UPDATE integrity_checks
        SET status = 'pending',
            consecutive_gaps = 0,
            details_json = COALESCE(details_json, '{}'::jsonb)
                || '{"reset_by":"0007_integrity_observation"}'::jsonb
        WHERE status = 'suspected_gap' OR consecutive_gaps > 0
        """
    )


def downgrade() -> None:
    # A downgrade cannot reconstruct false-positive counters safely.
    pass
