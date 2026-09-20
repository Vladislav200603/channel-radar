from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.models import Channel, ChannelMetricSnapshot, Post, PostMetricSnapshot
from app.services.telegram import ChannelUnavailable, ParsedChannel, fetch_channel

_collection_locks: dict[int, asyncio.Lock] = {}
FRESH_FETCH_BUDGET_SECONDS = 18.0
HISTORY_FETCH_BUDGET_SECONDS = 8.0
HISTORY_PAGES_PER_RUN = 2


def get_collection_lock(channel_id: int) -> asyncio.Lock:
    """Share the same lock with deletion and every collection entry point."""
    return _collection_locks.setdefault(channel_id, asyncio.Lock())


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
    fresh: bool = True,
) -> CollectionResult:
    settings = get_settings()
    now = observed_at or datetime.now(UTC)
    bucket = floor_to_bucket(now, settings.collection_bucket_minutes)

    if fresh:
        channel.title = parsed.title
        channel.description = parsed.description
        channel.subscriber_count = parsed.subscriber_count
        channel.status = "degraded" if channel.backfill_cursor else "healthy"
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
        views = [post.views for post in parsed.posts if post.views is not None]
        channel_snapshot.fresh_average_views = sum(views) / len(views) if views else None
        channel_snapshot.collected_at = now

    new_posts = 0
    updated_posts = 0
    existing_posts = {
        post.telegram_post_id: post
        for post in db.scalars(
            select(Post).where(
                Post.channel_id == channel.id,
                Post.telegram_post_id.in_([item.telegram_post_id for item in parsed.posts]),
            )
        )
    }
    for item in parsed.posts:
        post = existing_posts.get(item.telegram_post_id)
        if post is None:
            post = Post(
                channel_id=channel.id,
                telegram_post_id=item.telegram_post_id,
                url=item.url,
                published_at=item.published_at,
                first_seen_at=now,
            )
            db.add(post)
            existing_posts[item.telegram_post_id] = post
            new_posts += 1
        else:
            updated_posts += 1
        post.url = item.url
        post.text = item.text
        post.author = item.author
        post.media_kind = item.media_kind
        post.published_at = item.published_at
        post.last_seen_at = now

    db.flush()
    existing_snapshots = {
        snapshot.post_id: snapshot
        for snapshot in db.scalars(
            select(PostMetricSnapshot).where(
                PostMetricSnapshot.post_id.in_([post.id for post in existing_posts.values()]),
                PostMetricSnapshot.observed_bucket == bucket,
            )
        )
    }
    for item in parsed.posts:
        post = existing_posts[item.telegram_post_id]
        snapshot = existing_snapshots.get(post.id)
        if snapshot is None:
            snapshot = PostMetricSnapshot(post_id=post.id, observed_bucket=bucket)
            db.add(snapshot)
            existing_snapshots[post.id] = snapshot
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
            channel.status in {"healthy", "degraded"}
            and last_collected
            and datetime.now(UTC) - last_collected < timedelta(seconds=30)
        ):
            return CollectionResult(channel.username, 0, 0, channel.status)
        username = channel.username
        previous_status = channel.status
        previous_head = db.scalar(
            select(func.max(Post.telegram_post_id)).where(Post.channel_id == channel_id)
        )
        channel.status = "collecting"
        channel.last_error = None
        channel.last_attempt_at = datetime.now(UTC)
        db.commit()
    try:
        # No database transaction remains open during network I/O. Every run starts
        # from the live head, regardless of outstanding historical work.
        latest = await fetch_channel(
            username,
            timeout_seconds=settings.telegram_timeout_seconds,
            max_pages=1,
            total_timeout_seconds=FRESH_FETCH_BUDGET_SECONDS,
        )
        with SessionLocal() as db:
            channel = db.get(Channel, channel_id)
            if channel is None:
                return CollectionResult(username, 0, 0, "error", "channel not found")
            if (
                previous_head is not None
                and latest.posts
                and min(post.telegram_post_id for post in latest.posts) > previous_head
                and latest.previous_page is not None
            ):
                gap = {"cursor": latest.previous_page, "stop_id": previous_head}
                if channel.backfill_cursor is None:
                    channel.backfill_cursor = gap["cursor"]
                    channel.backfill_stop_id = previous_head
                else:
                    channel.pending_gaps = [*(channel.pending_gaps or []), gap]
            if not channel.archive_initialized:
                # Legacy channels are enrolled regardless of how many posts were
                # saved before migration. Overlap never proves archive completion.
                channel.archive_initialized = True
                channel.archive_cursor = latest.previous_page
                channel.archive_error = None
            result = persist_parsed_channel(db, channel, latest)
    except asyncio.CancelledError:
        with SessionLocal() as db:
            channel = db.get(Channel, channel_id)
            if channel is not None and channel.status == "collecting":
                channel.status = previous_status
                db.commit()
        raise
    except (ChannelUnavailable, httpx.HTTPError, ValueError, TimeoutError) as exc:
        with SessionLocal() as db:
            return _mark_collection_error(db, channel_id, str(exc) or "Telegram collection timed out")
    except Exception as exc:  # One source must not stop collection of other channels.
        with SessionLocal() as db:
            return _mark_collection_error(db, channel_id, f"collector failure: {type(exc).__name__}")

    # A newly added channel is usable after just one preview request. Further runs
    # spend independent, bounded budgets on recent gaps and the older archive.
    if previous_head is None:
        return result
    for archive in (False, True):
        with SessionLocal() as db:
            channel = db.get(Channel, channel_id)
            if channel is None:
                return result
            cursor = channel.archive_cursor if archive else channel.backfill_cursor
            stop_id = None if archive else channel.backfill_stop_id
        if cursor is None:
            continue
        try:
            historical = await fetch_channel(
                username,
                timeout_seconds=settings.telegram_timeout_seconds,
                start_page=cursor,
                stop_at_post_id=stop_id,
                full_history=archive,
                max_pages=HISTORY_PAGES_PER_RUN,
                total_timeout_seconds=HISTORY_FETCH_BUDGET_SECONDS,
            )
            with SessionLocal() as db:
                channel = db.get(Channel, channel_id)
                if channel is None:
                    return result
                if archive:
                    channel.archive_cursor = historical.resume_cursor
                    channel.archive_error = None
                else:
                    channel.backfill_cursor = historical.resume_cursor
                    if historical.history_complete:
                        pending = list(channel.pending_gaps or [])
                        next_gap = pending.pop(0) if pending else None
                        channel.backfill_cursor = next_gap["cursor"] if next_gap else None
                        channel.backfill_stop_id = next_gap["stop_id"] if next_gap else None
                        channel.pending_gaps = pending
                    channel.last_error = None
                    channel.status = "degraded" if channel.backfill_cursor else "healthy"
                saved = persist_parsed_channel(db, channel, historical, fresh=False)
                result.new_posts += saved.new_posts
                result.updated_posts += saved.updated_posts
                result.status = saved.status
        except asyncio.CancelledError:
            # The latest window and its timestamp are already committed; the cursor
            # still points at this unfinished batch and will be retried next time.
            raise
        except Exception as exc:
            with SessionLocal() as db:
                channel = db.get(Channel, channel_id)
                if channel is None:
                    return result
                detail = (
                    str(exc) or "Завантаження історії перевищило час очікування."
                    if isinstance(exc, (ChannelUnavailable, httpx.HTTPError, ValueError, TimeoutError))
                    else f"history collection failure: {type(exc).__name__}"
                )
                if archive:
                    channel.archive_error = detail[:1000]
                else:
                    channel.last_error = detail[:1000]
                    channel.status = "degraded"
                db.commit()
                result.status = channel.status
    return result


async def collect_channel_by_id(channel_id: int) -> CollectionResult:
    lock = get_collection_lock(channel_id)
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
