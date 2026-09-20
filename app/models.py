from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class Channel(Base):
    __tablename__ = "channels"
    __table_args__ = (UniqueConstraint("username"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    subscriber_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    last_error: Mapped[str | None] = mapped_column(Text)
    backfill_cursor: Mapped[str | None] = mapped_column(String(256))
    backfill_stop_id: Mapped[int | None] = mapped_column(Integer)
    pending_gaps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    archive_initialized: Mapped[bool] = mapped_column(Boolean, default=False)
    archive_cursor: Mapped[str | None] = mapped_column(String(256))
    archive_error: Mapped[str | None] = mapped_column(Text)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    posts: Mapped[list[Post]] = relationship(back_populates="channel", cascade="all, delete-orphan")
    snapshots: Mapped[list[ChannelMetricSnapshot]] = relationship(
        back_populates="channel", cascade="all, delete-orphan"
    )
    digests: Mapped[list[ChannelDigest]] = relationship(
        back_populates="channel", cascade="all, delete-orphan"
    )


class ChannelMetricSnapshot(Base):
    __tablename__ = "channel_metric_snapshots"
    __table_args__ = (
        UniqueConstraint("channel_id", "observed_bucket", name="uq_channel_snapshot_bucket"),
        Index("ix_channel_snapshots_channel_time", "channel_id", "observed_bucket"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"))
    observed_bucket: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    subscriber_count: Mapped[int | None] = mapped_column(Integer)
    posts_seen: Mapped[int] = mapped_column(Integer, default=0)
    fresh_average_views: Mapped[float | None] = mapped_column(Float)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    channel: Mapped[Channel] = relationship(back_populates="snapshots")


class Post(Base):
    __tablename__ = "posts"
    __table_args__ = (
        UniqueConstraint("channel_id", "telegram_post_id", name="uq_post_natural_key"),
        Index("ix_posts_channel_published", "channel_id", "published_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"))
    telegram_post_id: Mapped[int] = mapped_column(Integer)
    url: Mapped[str] = mapped_column(String(512))
    text: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str | None] = mapped_column(String(255))
    media_kind: Mapped[str | None] = mapped_column(String(32))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    channel: Mapped[Channel] = relationship(back_populates="posts")
    metric_snapshots: Mapped[list[PostMetricSnapshot]] = relationship(
        back_populates="post", cascade="all, delete-orphan"
    )


class PostMetricSnapshot(Base):
    __tablename__ = "post_metric_snapshots"
    __table_args__ = (
        UniqueConstraint("post_id", "observed_bucket", name="uq_post_snapshot_bucket"),
        Index("ix_post_snapshots_post_time", "post_id", "observed_bucket"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"))
    observed_bucket: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    views: Mapped[int | None] = mapped_column(Integer)
    forwards: Mapped[int | None] = mapped_column(Integer)
    reactions_total: Mapped[int | None] = mapped_column(Integer)
    reactions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    post: Mapped[Post] = relationship(back_populates="metric_snapshots")


class ChannelDigest(Base):
    __tablename__ = "channel_digests"
    __table_args__ = (Index("ix_digests_channel_created", "channel_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"))
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    content: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(String(32), default="google")
    model: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    channel: Mapped[Channel] = relationship(back_populates="digests")
