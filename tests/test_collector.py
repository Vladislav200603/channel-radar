from datetime import UTC, datetime

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


@pytest.mark.asyncio
async def test_collection_resumes_and_clears_saved_backfill_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    test_sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with test_sessions() as db:
        channel = Channel(
            username="durov",
            status="degraded",
            backfill_cursor="/s/durov?before=42",
        )
        db.add(channel)
        db.commit()
        channel_id = channel.id

    history = sample_payload(1_000)
    calls: list[str | None] = []

    async def fake_fetch_channel(*args: object, **kwargs: object) -> ParsedChannel:
        del args
        start_page = kwargs.get("start_page")
        calls.append(start_page if isinstance(start_page, str) else None)
        return history

    monkeypatch.setattr(collector, "SessionLocal", test_sessions)
    monkeypatch.setattr(collector, "fetch_channel", fake_fetch_channel)

    result = await collector._collect_channel_by_id(channel_id)

    assert calls == ["/s/durov?before=42"]
    assert result.status == "healthy"
    with test_sessions() as db:
        saved = db.get(Channel, channel_id)
        assert saved is not None
        assert saved.backfill_cursor is None
        assert db.scalar(select(func.count()).select_from(Post)) == 1
