from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.models import Channel, ChannelMetricSnapshot, Post, PostMetricSnapshot
from app.services.telegram import ChannelUnavailable, ParsedChannel, fetch_channel

_collection_locks: dict[int, asyncio.Lock] = {}


@dataclass(slots=True)
class CollectionResult:
    username: str
    new_posts: int
    updated_posts: int
    status: str
    error: str | None = None


def floor_to_bucket(value: datetime, minutes: int) -> datetime:
    value = value.astimezone(UTC)
    bucket_seconds = minutes * 60
    floored_timestamp = int(value.timestamp()) // bucket_seconds * bucket_seconds
    return datetime.fromtimestamp(floored_timestamp, tz=UTC)


def persist_parsed_channel(
    db: Session,
    channel: Channel,
    parsed: ParsedChannel,
    *,
    observed_at: datetime | None = None,
) -> CollectionResult:
    settings = get_settings()
    now = observed_at or datetime.now(UTC)
    bucket = floor_to_bucket(now, settings.collection_bucket_minutes)

    channel.title = parsed.title
    channel.description = parsed.description
    channel.subscriber_count = parsed.subscriber_count
    channel.status = "healthy" if parsed.history_complete else "degraded"
    channel.backfill_cursor = parsed.resume_cursor
    channel.last_error = None
    channel.last_attempt_at = now
    channel.last_collected_at = now

    channel_snapshot = db.scalar(
        select(ChannelMetricSnapshot).where(
            ChannelMetricSnapshot.channel_id == channel.id,
            ChannelMetricSnapshot.observed_bucket == bucket,
        )
    )
    if channel_snapshot is None:
        channel_snapshot = ChannelMetricSnapshot(channel_id=channel.id, observed_bucket=bucket)
        db.add(channel_snapshot)
    channel_snapshot.subscriber_count = parsed.subscriber_count
    channel_snapshot.posts_seen = len(parsed.posts)
    channel_snapshot.collected_at = now

    new_posts = 0
    updated_posts = 0
    for item in parsed.posts:
        post = db.scalar(
            select(Post).where(
                Post.channel_id == channel.id,
                Post.telegram_post_id == item.telegram_post_id,
            )
        )
        if post is None:
            post = Post(
                channel_id=channel.id,
                telegram_post_id=item.telegram_post_id,
                url=item.url,
                published_at=item.published_at,
                first_seen_at=now,
            )
            db.add(post)
            db.flush()
            new_posts += 1
        else:
            updated_posts += 1
        post.url = item.url
        post.text = item.text
        post.author = item.author
        post.media_kind = item.media_kind
        post.published_at = item.published_at
        post.last_seen_at = now

        snapshot = db.scalar(
            select(PostMetricSnapshot).where(
                PostMetricSnapshot.post_id == post.id,
                PostMetricSnapshot.observed_bucket == bucket,
            )
        )
        if snapshot is None:
            snapshot = PostMetricSnapshot(post_id=post.id, observed_bucket=bucket)
            db.add(snapshot)
        snapshot.views = item.views
        snapshot.forwards = item.forwards
        snapshot.reactions_total = item.reactions_total
        snapshot.reactions = item.reactions
        snapshot.collected_at = now

    db.commit()
    return CollectionResult(channel.username, new_posts, updated_posts, channel.status)


def _mark_collection_error(db: Session, channel_id: int, error: str) -> CollectionResult:
    db.rollback()
    channel = db.get(Channel, channel_id)
    if channel is None:
        return CollectionResult("unknown", 0, 0, "error", error)
    channel.status = "error"
    channel.last_error = error[:1000]
    channel.last_attempt_at = datetime.now(UTC)
    db.commit()
    return CollectionResult(channel.username, 0, 0, "error", error)


async def _collect_channel_by_id(channel_id: int) -> CollectionResult:
    settings = get_settings()
    with SessionLocal() as db:
        channel = db.get(Channel, channel_id)
        if channel is None:
            return CollectionResult("unknown", 0, 0, "error", "channel not found")
        last_collected = channel.last_collected_at
        if last_collected and last_collected.tzinfo is None:
            last_collected = last_collected.replace(tzinfo=UTC)
        if (
            channel.status == "healthy"
            and last_collected
            and datetime.now(UTC) - last_collected < timedelta(seconds=30)
        ):
            return CollectionResult(channel.username, 0, 0, "healthy")
        channel.status = "collecting"
        channel.last_error = None
        channel.last_attempt_at = datetime.now(UTC)
        db.commit()
        try:
            known_post_ids = set(
                db.scalars(select(Post.telegram_post_id).where(Post.channel_id == channel.id))
            )
            # Finish an existing history gap before opening another one. The latest
            # window was already persisted when this cursor was created; prioritising
            # one cursor at a time prevents a high-volume channel from losing gaps.
            parsed = await fetch_channel(
                channel.username,
                timeout_seconds=settings.telegram_timeout_seconds,
                known_post_ids=known_post_ids,
                start_page=channel.backfill_cursor,
            )
            return persist_parsed_channel(db, channel, parsed)
        except (ChannelUnavailable, httpx.HTTPError, ValueError) as exc:
            return _mark_collection_error(db, channel_id, str(exc))
        except Exception as exc:  # Defensive boundary: one source must not stop the collector.
            return _mark_collection_error(db, channel_id, f"unexpected collector error: {exc}")


async def collect_channel_by_id(channel_id: int) -> CollectionResult:
    lock = _collection_locks.setdefault(channel_id, asyncio.Lock())
    async with lock:
        return await _collect_channel_by_id(channel_id)


async def collect_all_channels() -> list[CollectionResult]:
    with SessionLocal() as db:
        channel_ids = list(db.scalars(select(Channel.id).order_by(Channel.id)))
    semaphore = asyncio.Semaphore(3)

    async def bounded_collect(channel_id: int) -> CollectionResult:
        async with semaphore:
            return await collect_channel_by_id(channel_id)

    return list(await asyncio.gather(*(bounded_collect(channel_id) for channel_id in channel_ids)))
