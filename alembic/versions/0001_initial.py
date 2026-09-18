"""Initial Channel Radar schema.

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "channels",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(32), nullable=False),
        sa.Column("title", sa.String(255)),
        sa.Column("description", sa.Text()),
        sa.Column("subscriber_count", sa.Integer()),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("last_error", sa.Text()),
        sa.Column("last_collected_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("username"),
    )
    op.create_index("ix_channels_username", "channels", ["username"])
    op.create_index("ix_channels_status", "channels", ["status"])

    op.create_table(
        "channel_metric_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "channel_id",
            sa.Integer(),
            sa.ForeignKey("channels.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("observed_bucket", sa.DateTime(timezone=True), nullable=False),
        sa.Column("subscriber_count", sa.Integer()),
        sa.Column("posts_seen", sa.Integer(), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("channel_id", "observed_bucket", name="uq_channel_snapshot_bucket"),
    )
    op.create_index(
        "ix_channel_snapshots_channel_time", "channel_metric_snapshots", ["channel_id", "observed_bucket"]
    )

    op.create_table(
        "posts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "channel_id",
            sa.Integer(),
            sa.ForeignKey("channels.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("telegram_post_id", sa.Integer(), nullable=False),
        sa.Column("url", sa.String(512), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("author", sa.String(255)),
        sa.Column("media_kind", sa.String(32)),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("channel_id", "telegram_post_id", name="uq_post_natural_key"),
    )
    op.create_index("ix_posts_published_at", "posts", ["published_at"])
    op.create_index("ix_posts_channel_published", "posts", ["channel_id", "published_at"])

    op.create_table(
        "post_metric_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("post_id", sa.Integer(), sa.ForeignKey("posts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("observed_bucket", sa.DateTime(timezone=True), nullable=False),
        sa.Column("views", sa.Integer()),
        sa.Column("forwards", sa.Integer()),
        sa.Column("reactions_total", sa.Integer()),
        sa.Column("reactions", sa.JSON(), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("post_id", "observed_bucket", name="uq_post_snapshot_bucket"),
    )
    op.create_index("ix_post_snapshots_post_time", "post_metric_snapshots", ["post_id", "observed_bucket"])

    op.create_table(
        "channel_digests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "channel_id",
            sa.Integer(),
            sa.ForeignKey("channels.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_digests_channel_created", "channel_digests", ["channel_id", "created_at"])


def downgrade() -> None:
    op.drop_table("channel_digests")
    op.drop_table("post_metric_snapshots")
    op.drop_table("posts")
    op.drop_table("channel_metric_snapshots")
    op.drop_table("channels")
