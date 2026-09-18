"""Track attempts and resumable Telegram history backfill.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("channels", sa.Column("backfill_cursor", sa.String(256), nullable=True))
    op.add_column("channels", sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("channels", "last_attempt_at")
    op.drop_column("channels", "backfill_cursor")
