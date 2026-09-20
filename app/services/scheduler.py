from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Channel
from app.services.collector import CollectionResult, collect_channel_by_id

logger = logging.getLogger(__name__)
INITIAL_FAILURE_RETRY_SECONDS = 6 * 60 * 60
_collection_batch_lock = asyncio.Lock()


def _is_due(channel: Channel, now: datetime, min_age_seconds: float) -> bool:
    timestamps = [value for value in (channel.last_attempt_at, channel.last_collected_at) if value]
    if not timestamps:
        return True
    last_activity = max(
        value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        for value in timestamps
    )
    retry_seconds = min_age_seconds
    if channel.status == "error" and channel.last_collected_at is None:
        # A first failure may be a typo/private channel OR a temporary network error.
        # Back off without permanently abandoning a valid channel after an outage.
        retry_seconds = max(retry_seconds, INITIAL_FAILURE_RETRY_SECONDS)
    return now - last_activity >= timedelta(seconds=retry_seconds)


async def collect_due_channels(
    *,
    now: datetime | None = None,
    min_age_seconds: float = 25 * 60,
    max_concurrency: int = 3,
) -> list[CollectionResult]:
    """Serialize startup/cron sweeps and check freshness after acquiring their lock."""
    async with _collection_batch_lock:
        return await _collect_due_channels(
            now=now, min_age_seconds=min_age_seconds, max_concurrency=max_concurrency
        )


async def _collect_due_channels(
    *,
    now: datetime | None,
    min_age_seconds: float,
    max_concurrency: int,
) -> list[CollectionResult]:
    """Do not hold DB sessions across network awaits or per-channel lock waits."""
    if min_age_seconds < 0 or max_concurrency < 1:
        raise ValueError("invalid scheduler collection limits")
    checked_at = now or datetime.now(UTC)
    with SessionLocal() as db:
        due_ids = [
            channel.id
            for channel in db.scalars(select(Channel).order_by(Channel.id))
            if _is_due(channel, checked_at, min_age_seconds)
        ]
    semaphore = asyncio.Semaphore(max_concurrency)

    async def collect_one(channel_id: int) -> CollectionResult | None:
        async with semaphore:
            username = "unknown"
            try:
                # A manual refresh or cron run may have handled this channel while
                # it waited for a slot. Close the session before entering its lock.
                with SessionLocal() as db:
                    channel = db.get(Channel, channel_id)
                    if channel is None or not _is_due(
                        channel, now or datetime.now(UTC), min_age_seconds
                    ):
                        return None
                    username = channel.username
                return await collect_channel_by_id(channel_id)
            except Exception as exc:
                # Collector normally contains source failures; this also isolates
                # unexpected database/adapter failures and never logs secret values.
                logger.warning(
                    "Automatic collection failed for channel %s (%s)",
                    channel_id,
                    type(exc).__name__,
                )
                return CollectionResult(username, 0, 0, "error", "automatic collection failed")

    results = await asyncio.gather(*(collect_one(channel_id) for channel_id in due_ids))
    return [result for result in results if result is not None]


async def run_collection_scheduler(
    stop_event: asyncio.Event,
    *,
    interval_seconds: float = 30 * 60,
    cycle_timeout_seconds: float = 180,
) -> None:
    """Catch up on startup and periodically while the single web process is awake."""
    if interval_seconds <= 0 or cycle_timeout_seconds <= 0:
        raise ValueError("scheduler intervals must be positive")
    while not stop_event.is_set():
        try:
            async with asyncio.timeout(cycle_timeout_seconds):
                results = await collect_due_channels()
            if results:
                logger.info(
                    "Automatic collection finished: collected=%s healthy=%s degraded=%s errors=%s",
                    len(results),
                    sum(result.status == "healthy" for result in results),
                    sum(result.status == "degraded" for result in results),
                    sum(result.status == "error" for result in results),
                )
        except TimeoutError:
            logger.warning("Automatic collection exceeded its cycle deadline")
        except Exception as exc:
            logger.warning("Automatic collection cycle failed (%s)", type(exc).__name__)

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass
