import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Channel, ChannelMetricSnapshot, Post, PostMetricSnapshot
from app.services import collector
from app.services.collector import floor_to_bucket, persist_parsed_channel
from app.services.telegram import ParsedChannel, ParsedPost


def sample_payload(views: int) -> ParsedChannel:
    return ParsedChannel(
        username="durov",
        title="Pavel Durov",
        description="Test",
        subscriber_count=10_700_000,
        posts=[
            ParsedPost(
                telegram_post_id=528,
                url="https://t.me/durov/528",
                published_at=datetime(2026, 6, 15, 18, 58, tzinfo=UTC),
                text="Hello",
                views=views,
                reactions={"paid_star": 100},
                reactions_available=True,
            )
        ],
    )


def test_bucket_floor_is_epoch_aligned_for_multi_hour_intervals() -> None:
    assert floor_to_bucket(datetime(2026, 9, 16, 10, 50, tzinfo=UTC), 90) == datetime(
        2026, 9, 16, 10, 30, tzinfo=UTC
    )
    assert floor_to_bucket(datetime(2026, 9, 16, 11, 10, tzinfo=UTC), 90) == datetime(
        2026, 9, 16, 10, 30, tzinfo=UTC
    )
    assert floor_to_bucket(datetime(2026, 9, 16, 23, 59, tzinfo=UTC), 1440) == datetime(
        2026, 9, 16, 0, 0, tzinfo=UTC
    )


def test_repeated_collection_is_idempotent_inside_bucket() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        channel = Channel(username="durov")
        db.add(channel)
        db.commit()

        first = persist_parsed_channel(
            db, channel, sample_payload(1_000), observed_at=datetime(2026, 9, 16, 10, 5, tzinfo=UTC)
        )
        second = persist_parsed_channel(
            db, channel, sample_payload(1_500), observed_at=datetime(2026, 9, 16, 10, 35, tzinfo=UTC)
        )

        assert first.new_posts == 1
        assert second.new_posts == 0
        assert db.scalar(select(func.count()).select_from(Post)) == 1
        assert db.scalar(select(func.count()).select_from(ChannelMetricSnapshot)) == 1
        assert db.scalar(select(func.count()).select_from(PostMetricSnapshot)) == 1
        snapshot = db.scalar(select(PostMetricSnapshot))
        assert snapshot.views == 1_500


def test_new_bucket_adds_metric_point_but_not_duplicate_post() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        channel = Channel(username="durov")
        db.add(channel)
        db.commit()
        persist_parsed_channel(
            db, channel, sample_payload(1_000), observed_at=datetime(2026, 9, 16, 10, 5, tzinfo=UTC)
        )
        persist_parsed_channel(
            db, channel, sample_payload(2_000), observed_at=datetime(2026, 9, 16, 11, 5, tzinfo=UTC)
        )

        assert db.scalar(select(func.count()).select_from(Post)) == 1
        assert db.scalar(select(func.count()).select_from(PostMetricSnapshot)) == 2


@pytest.fixture
def collection_db(monkeypatch: pytest.MonkeyPatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    test_sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(collector, "SessionLocal", test_sessions)
    yield test_sessions
    engine.dispose()


def payload(ids: list[int], *, cursor: str | None = None, views: int = 100) -> ParsedChannel:
    parsed = sample_payload(views)
    parsed.posts = [
        ParsedPost(
            telegram_post_id=post_id,
            url=f"https://t.me/durov/{post_id}",
            published_at=datetime(2026, 9, 19, 10, tzinfo=UTC),
            text=f"Post {post_id}",
            views=views,
        )
        for post_id in ids
    ]
    parsed.previous_page = cursor
    parsed.resume_cursor = cursor
    parsed.history_complete = cursor is None
    return parsed


def seed_channel(sessions, ids: list[int], **channel_values) -> int:
    with sessions() as db:
        channel = Channel(username="durov", **channel_values)
        db.add(channel)
        db.commit()
        if ids:
            persist_parsed_channel(
                db, channel, payload(ids), observed_at=datetime.now(UTC) - timedelta(minutes=5)
            )
        return channel.id


@pytest.mark.asyncio
async def test_fresh_posts_commit_before_independent_archive(collection_db, monkeypatch) -> None:
    channel_id = seed_channel(
        collection_db, [20, 19], archive_initialized=True, archive_cursor="/s/durov?before=19"
    )
    calls = []

    async def fake_fetch(username, **kwargs):
        cursor = kwargs.get("start_page")
        calls.append(cursor)
        if cursor is None:
            return payload([21, 20], cursor="/s/durov?before=20", views=200)
        with collection_db() as db:
            assert db.scalar(select(Post).where(Post.telegram_post_id == 21)) is not None
            assert db.get(Channel, channel_id).status == "healthy"
        assert kwargs["full_history"] is True
        return payload([18, 17], cursor="/s/durov?before=17", views=10_000)

    monkeypatch.setattr(collector, "fetch_channel", fake_fetch)
    result = await collector._collect_channel_by_id(channel_id)

    assert calls == [None, "/s/durov?before=19"]
    assert result.status == "healthy"
    with collection_db() as db:
        saved = db.get(Channel, channel_id)
        assert saved.archive_cursor == "/s/durov?before=17"
        snapshot = db.scalar(
            select(ChannelMetricSnapshot).order_by(ChannelMetricSnapshot.observed_bucket.desc())
        )
        assert snapshot.fresh_average_views == 200
        assert snapshot.posts_seen == 2


@pytest.mark.asyncio
async def test_archive_failure_preserves_latest_posts_and_cursor(collection_db, monkeypatch) -> None:
    channel_id = seed_channel(
        collection_db, [20, 19], archive_initialized=True, archive_cursor="/s/durov?before=19"
    )

    async def fake_fetch(username, **kwargs):
        if kwargs.get("start_page") is None:
            return payload([21, 20], cursor="/s/durov?before=20")
        raise httpx.ReadTimeout("archive timeout")

    monkeypatch.setattr(collector, "fetch_channel", fake_fetch)
    result = await collector._collect_channel_by_id(channel_id)

    assert result.status == "healthy"
    with collection_db() as db:
        saved = db.get(Channel, channel_id)
        assert saved.archive_cursor == "/s/durov?before=19"
        assert saved.archive_error == "archive timeout"
        assert saved.last_error is None
        assert saved.last_collected_at > datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=10)
        assert db.scalar(select(Post).where(Post.telegram_post_id == 21)) is not None


@pytest.mark.asyncio
async def test_legacy_channel_with_40_posts_is_enrolled_and_resumed(collection_db, monkeypatch) -> None:
    channel_id = seed_channel(collection_db, list(range(61, 101)))
    calls = []

    async def fake_fetch(username, **kwargs):
        cursor = kwargs.get("start_page")
        calls.append(cursor)
        if cursor is None:
            return payload([101, 100], cursor="/s/durov?before=100")
        assert kwargs["full_history"] is True
        return payload([99, 98], cursor="/s/durov?before=98")

    monkeypatch.setattr(collector, "fetch_channel", fake_fetch)
    await collector._collect_channel_by_id(channel_id)

    assert calls == [None, "/s/durov?before=100"]
    with collection_db() as db:
        saved = db.get(Channel, channel_id)
        assert saved.archive_initialized
        assert saved.archive_cursor == "/s/durov?before=98"
        assert db.scalar(select(func.count()).select_from(Post)) == 41


@pytest.mark.asyncio
async def test_new_channel_needs_only_one_preview_request(collection_db, monkeypatch) -> None:
    channel_id = seed_channel(collection_db, [])
    calls = []

    async def fake_fetch(username, **kwargs):
        calls.append(kwargs.get("start_page"))
        return payload([20, 19], cursor="/s/durov?before=19")

    monkeypatch.setattr(collector, "fetch_channel", fake_fetch)
    result = await collector._collect_channel_by_id(channel_id)
    assert calls == [None]
    assert result.status == "healthy"
    with collection_db() as db:
        assert db.get(Channel, channel_id).archive_cursor == "/s/durov?before=19"


@pytest.mark.asyncio
async def test_new_recent_gap_does_not_replace_unfinished_gap(collection_db, monkeypatch) -> None:
    channel_id = seed_channel(
        collection_db,
        [100],
        archive_initialized=True,
        backfill_cursor="/s/durov?before=80",
        backfill_stop_id=50,
    )
    calls = []

    async def fake_fetch(username, **kwargs):
        cursor = kwargs.get("start_page")
        calls.append((cursor, kwargs.get("stop_at_post_id")))
        if cursor is None:
            return payload([200, 199], cursor="/s/durov?before=199")
        if cursor == "/s/durov?before=80":
            assert kwargs["stop_at_post_id"] == 50
            return payload([79, 50])
        assert cursor == "/s/durov?before=199"
        assert kwargs["stop_at_post_id"] == 100
        return payload([198, 100])

    monkeypatch.setattr(collector, "fetch_channel", fake_fetch)
    result = await collector._collect_channel_by_id(channel_id)
    assert result.status == "degraded"
    with collection_db() as db:
        saved = db.get(Channel, channel_id)
        assert saved.backfill_cursor == "/s/durov?before=199"
        assert saved.backfill_stop_id == 100
        assert saved.pending_gaps == []
        saved.last_collected_at = datetime.now(UTC) - timedelta(minutes=5)
        db.commit()

    result = await collector._collect_channel_by_id(channel_id)
    assert result.status == "healthy"
    with collection_db() as db:
        saved = db.get(Channel, channel_id)
        assert saved.backfill_cursor is None
        assert saved.backfill_stop_id is None
        assert saved.pending_gaps == []
        assert db.scalar(select(func.count()).select_from(Post)) == 6
    assert calls == [
        (None, None), ("/s/durov?before=80", 50), (None, None), ("/s/durov?before=199", 100)
    ]


@pytest.mark.asyncio
async def test_history_retry_is_idempotent_inside_bucket(collection_db, monkeypatch) -> None:
    channel_id = seed_channel(
        collection_db, [20], archive_initialized=True, archive_cursor="/s/durov?before=19"
    )

    async def fake_fetch(username, **kwargs):
        if kwargs.get("start_page") is None:
            return payload([21, 20], cursor="/s/durov?before=20")
        return payload([18, 17], cursor="/s/durov?before=17")

    monkeypatch.setattr(collector, "fetch_channel", fake_fetch)
    await collector._collect_channel_by_id(channel_id)
    with collection_db() as db:
        count_before = db.scalar(select(func.count()).select_from(PostMetricSnapshot))
        channel = db.get(Channel, channel_id)
        channel.archive_cursor = "/s/durov?before=19"
        channel.last_collected_at = datetime.now(UTC) - timedelta(minutes=5)
        db.commit()

    result = await collector._collect_channel_by_id(channel_id)
    assert result.new_posts == 0
    with collection_db() as db:
        assert db.scalar(select(func.count()).select_from(PostMetricSnapshot)) == count_before
        assert db.scalar(select(func.count()).select_from(Post)) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_history", [False, True])
async def test_cancellation_never_leaves_channel_collecting(
    collection_db, monkeypatch, cancel_history
) -> None:
    channel_id = seed_channel(
        collection_db, [20], archive_initialized=True, archive_cursor="/s/durov?before=19"
    )

    async def fake_fetch(username, **kwargs):
        if cancel_history and kwargs.get("start_page") is None:
            return payload([21, 20], cursor="/s/durov?before=20")
        raise asyncio.CancelledError

    monkeypatch.setattr(collector, "fetch_channel", fake_fetch)
    with pytest.raises(asyncio.CancelledError):
        await collector._collect_channel_by_id(channel_id)
    with collection_db() as db:
        saved = db.get(Channel, channel_id)
        assert saved.status == "healthy"
        assert saved.archive_cursor == "/s/durov?before=19"
        if cancel_history:
            assert db.scalar(select(Post).where(Post.telegram_post_id == 21)) is not None


@pytest.mark.asyncio
async def test_recent_gap_failure_keeps_queued_work_and_fresh_data(collection_db, monkeypatch) -> None:
    channel_id = seed_channel(
        collection_db,
        [100],
        archive_initialized=True,
        backfill_cursor="/s/durov?before=80",
        backfill_stop_id=50,
    )

    async def fake_fetch(username, **kwargs):
        if kwargs.get("start_page") is None:
            return payload([200, 199], cursor="/s/durov?before=199")
        raise httpx.ReadTimeout("gap timeout")

    monkeypatch.setattr(collector, "fetch_channel", fake_fetch)
    result = await collector._collect_channel_by_id(channel_id)
    assert result.status == "degraded"
    with collection_db() as db:
        saved = db.get(Channel, channel_id)
        assert saved.backfill_cursor == "/s/durov?before=80"
        assert saved.backfill_stop_id == 50
        assert saved.pending_gaps == [{"cursor": "/s/durov?before=199", "stop_id": 100}]
        assert db.scalar(select(Post).where(Post.telegram_post_id == 200)) is not None
