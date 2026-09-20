"""Separate fresh collection, recent gaps, and archival progress.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("channels", sa.Column("backfill_stop_id", sa.Integer(), nullable=True))
    op.add_column("channels", sa.Column("pending_gaps", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column(
        "channels",
        sa.Column("archive_initialized", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("channels", sa.Column("archive_cursor", sa.String(256), nullable=True))
    op.add_column("channels", sa.Column("archive_error", sa.Text(), nullable=True))
    op.add_column(
        "channel_metric_snapshots", sa.Column("fresh_average_views", sa.Float(), nullable=True)
    )
    # The old cursor represented both archive work and recent gaps, with no durable
    # boundary between them. Re-enrol every existing channel from its latest page;
    # existing posts are kept and upserted as the archive passes through them.
    op.execute("UPDATE channels SET backfill_cursor = NULL")


def downgrade() -> None:
    op.drop_column("channel_metric_snapshots", "fresh_average_views")
    op.drop_column("channels", "archive_error")
    op.drop_column("channels", "archive_cursor")
    op.drop_column("channels", "archive_initialized")
    op.drop_column("channels", "pending_gaps")
    op.drop_column("channels", "backfill_stop_id")
