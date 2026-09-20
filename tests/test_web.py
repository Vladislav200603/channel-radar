import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from bs4 import BeautifulSoup
from sqlalchemy import create_engine, event, func, select, update
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import main
from app.database import Base, get_db
from app.models import Channel, ChannelDigest, ChannelMetricSnapshot, Post, PostMetricSnapshot
from app.services import collector
from app.services.ai import DigestResult

COMPLETE_DIGEST = (
    "Підсумок: Канал цього тижня повідомляв про оновлення продукту та майбутню зустріч. "
    "Автор також відповідав на запитання читачів і уточнював план наступних публікацій.\n\n"
    "Головні теми:\n- Оновлення продукту та виправлення помилок.\n"
    "- Підготовка до зустрічі з підписниками.\n- Відповіді на запитання спільноти.\n\n"
    "Спостереження: Тон публікацій залишається інформаційним. Надано лише текстову вибірку, "
    "тому про зміст відео чи повну історію каналу висновків зробити не можна."
)


@pytest.fixture
def sessions(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override_db():
        with factory() as db:
            yield db

    main.app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(collector, "SessionLocal", factory)
    monkeypatch.setattr(main, "_digest_locks", {})
    monkeypatch.setattr(collector, "_collection_locks", {})
    yield factory
    main.app.dependency_overrides.clear()
    engine.dispose()


@pytest.fixture
async def client(sessions):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://test"
    ) as client:
        yield client


def seed_channel(sessions, username="example", post_count=1, **kwargs):
    now = datetime.now(UTC)
    with sessions() as db:
        channel = Channel(username=username, title=username, status="healthy", **kwargs)
        db.add(channel)
        db.flush()
        channel_id = channel.id
        for number in range(post_count):
            post = Post(
                channel_id=channel_id,
                telegram_post_id=number + 1,
                url=f"https://t.me/{username}/{number + 1}",
                text=f"Post {number + 1}",
                published_at=now - timedelta(minutes=number),
            )
            db.add(post)
            db.flush()
            db.add(PostMetricSnapshot(post_id=post.id, observed_bucket=now, views=number + 20))
        db.add(ChannelMetricSnapshot(channel_id=channel_id, observed_bucket=now, subscriber_count=10))
        db.add(
            ChannelDigest(
                channel_id=channel_id,
                period_start=now - timedelta(days=7),
                period_end=now,
                content=COMPLETE_DIGEST,
                model="test-model",
            )
        )
        db.commit()
        return channel_id


async def confirmation(client, username):
    response = await client.get(f"/channels/{username}/delete")
    assert response.status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    return soup.select_one('input[name="confirmation_token"]')["value"]


@pytest.mark.asyncio
async def test_delete_is_confirmed_cascades_and_preserves_other_channels(client, sessions):
    removed_id = seed_channel(sessions)
    kept_id = seed_channel(sessions, "keepchannel")
    token = await confirmation(client, "example")
    with sessions() as db:
        assert db.get(Channel, removed_id) is not None  # GET is always read-only.
    response = await client.post("/channels/example/delete", data={"confirmation_token": token})
    assert response.status_code == 303
    assert response.headers["location"] == "/?removed=example"
    with sessions() as db:
        assert db.get(Channel, removed_id) is None
        assert db.get(Channel, kept_id) is not None
        for model in (Post, PostMetricSnapshot, ChannelMetricSnapshot, ChannelDigest):
            assert db.scalar(select(func.count()).select_from(model)) == 1


@pytest.mark.asyncio
async def test_delete_rejects_forged_or_wrong_channel_confirmation(client, sessions):
    seed_channel(sessions)
    seed_channel(sessions, "keepchannel")
    response = await client.post("/channels/example/delete", data={"confirmation_token": "forged"})
    assert response.status_code == 403
    token = await confirmation(client, "example")
    response = await client.post("/channels/keepchannel/delete", data={"confirmation_token": token})
    assert response.status_code == 403
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Channel)) == 2


@pytest.mark.asyncio
async def test_delete_releases_full_limit_for_new_channel(client, sessions):
    with sessions() as db:
        db.add_all(Channel(username=f"invalid{i}", status="error") for i in range(10))
        db.commit()
    blocked = await client.post("/channels", data={"channel": "durov"})
    assert "error=" in blocked.headers["location"]
    token = await confirmation(client, "invalid0")
    await client.post("/channels/invalid0/delete", data={"confirmation_token": token})
    response = await client.post("/channels", data={"channel": "durov"})
    assert response.headers["location"] == "/channels/durov?start=1"


@pytest.mark.asyncio
async def test_deletion_waits_for_active_collection(client, sessions):
    channel_id = seed_channel(sessions)
    token = await confirmation(client, "example")
    lock = collector.get_collection_lock(channel_id)
    await lock.acquire()
    deleting = asyncio.create_task(
        client.post("/channels/example/delete", data={"confirmation_token": token})
    )
    await asyncio.sleep(0.03)
    assert not deleting.done()
    lock.release()
    assert (await deleting).status_code == 303


@pytest.mark.asyncio
async def test_channel_history_paginates_and_batches_metrics(client, sessions):
    seed_channel(sessions, post_count=120)
    queries = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        queries.append(statement)

    engine = sessions.kw["bind"]
    event.listen(engine, "before_cursor_execute", capture)
    try:
        first = await client.get("/channels/example?days=7")
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    soup = BeautifulSoup(first.text, "html.parser")
    assert len(soup.select("a.post-link")) == 50
    assert "120" in soup.select(".stat-card")[1].get_text()
    assert soup.select_one('a[href="?days=7&page=2"]') is not None
    assert len(queries) <= 8
    third = await client.get("/channels/example?days=7&page=3")
    third_soup = BeautifulSoup(third.text, "html.parser")
    assert len(third_soup.select("a.post-link")) == 20
    assert not {a["href"] for a in soup.select("a.post-link")} & {
        a["href"] for a in third_soup.select("a.post-link")
    }


@pytest.mark.asyncio
async def test_refresh_waits_for_result_and_keeps_period(client, sessions, monkeypatch):
    seed_channel(sessions)
    finished = False

    async def collect(_id):
        nonlocal finished
        await asyncio.sleep(0)
        finished = True
        return collector.CollectionResult("example", 0, 0, "healthy")

    monkeypatch.setattr(main, "collect_channel_by_id", collect)
    response = await client.post("/channels/example/refresh", data={"days": "90"})
    assert finished
    assert response.headers["location"] == "/channels/example?days=90"
    detail = await client.get(response.headers["location"])
    assert "refreshForm.addEventListener('submit'" in detail.text
    assert "window.location.replace('/channels/example?days=90')" in detail.text


@pytest.mark.asyncio
async def test_stale_status_matches_overview_and_detail(client, sessions):
    seed_channel(sessions, last_collected_at=datetime.now(UTC) - timedelta(hours=4))
    assert "status-stale" in (await client.get("/")).text
    assert "status-stale" in (await client.get("/channels/example")).text


@pytest.mark.asyncio
async def test_invalid_saved_digest_does_not_hide_last_good_one(client, sessions):
    channel_id = seed_channel(sessions)
    with sessions() as db:
        db.add(
            ChannelDigest(
                channel_id=channel_id,
                period_start=datetime.now(UTC) - timedelta(days=7),
                period_end=datetime.now(UTC),
                content="Digest intro.\n### 1.",
                model="broken",
            )
        )
        db.commit()
    response = await client.get("/channels/example")
    assert "test-model" in response.text
    assert "Digest intro." not in response.text


@pytest.mark.asyncio
async def test_failed_digest_never_overwrites_previous_complete_result(client, sessions, monkeypatch):
    channel_id = seed_channel(sessions)
    with sessions() as db:
        digest = db.scalar(select(ChannelDigest).where(ChannelDigest.channel_id == channel_id))
        digest.created_at = datetime.now(UTC) - timedelta(hours=1)
        db.commit()

    async def fail(*_args, **_kwargs):
        return DigestResult(False, error="AI тимчасово недоступний")

    monkeypatch.setattr(main.GeminiDigestService, "generate", fail)
    response = await client.post("/channels/example/digest")
    assert "ai_error=" in response.headers["location"]
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(ChannelDigest)) == 1
        assert db.scalar(select(ChannelDigest.content)) == COMPLETE_DIGEST


@pytest.mark.asyncio
async def test_last_history_page_uses_whole_period_for_anomaly_signal(client, sessions):
    channel_id = seed_channel(sessions, post_count=51)
    with sessions() as db:
        db.execute(update(PostMetricSnapshot).values(views=100))
        oldest = db.scalar(select(Post.id).where(Post.channel_id == channel_id, Post.telegram_post_id == 51))
        db.execute(update(PostMetricSnapshot).where(PostMetricSnapshot.post_id == oldest).values(views=1000))
        db.commit()
    response = await client.get("/channels/example?days=7&page=2")
    soup = BeautifulSoup(response.text, "html.parser")
    assert len(soup.select("a.post-link")) == 1
    assert soup.select_one(".signal-hot").get_text(strip=True) == "↑ 10.0×"


@pytest.mark.asyncio
async def test_deletion_waits_for_active_digest(client, sessions):
    channel_id = seed_channel(sessions)
    token = await confirmation(client, "example")
    lock = main._digest_locks.setdefault(channel_id, asyncio.Lock())
    await lock.acquire()
    deleting = asyncio.create_task(
        client.post("/channels/example/delete", data={"confirmation_token": token})
    )
    await asyncio.sleep(0.03)
    assert not deleting.done()
    lock.release()
    assert (await deleting).status_code == 303


@pytest.mark.asyncio
async def test_simultaneous_additions_keep_ten_channel_limit(client, sessions):
    with sessions() as db:
        db.add_all(Channel(username=f"existing{i}", status="pending") for i in range(9))
        db.commit()
    results = await asyncio.gather(
        *(client.post("/channels", data={"channel": f"newchannel{i}"}) for i in range(5))
    )
    assert sum("start=1" in response.headers["location"] for response in results) == 1
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Channel)) == 10


@pytest.mark.asyncio
async def test_cron_reports_existing_errors_even_if_startup_just_attempted_them(
    client, sessions, monkeypatch
):
    monkeypatch.setattr(main.settings, "cron_secret", "test-cron-secret")
    with sessions() as db:
        db.add(Channel(username="invalid", status="error", last_attempt_at=datetime.now(UTC)))
        db.commit()

    async def nothing_due():
        return []

    monkeypatch.setattr(main, "collect_due_channels", nothing_due)
    unauthorized = await client.post("/internal/collect-all")
    assert unauthorized.status_code == 401
    response = await client.post(
        "/internal/collect-all", headers={"Authorization": "Bearer test-cron-secret"}
    )
    assert response.status_code == 200
    assert response.json() == {
        "collected": 0,
        "healthy": 0,
        "backfilling": 0,
        "errors": [],
        "source_healthy": 0,
        "source_errors": ["invalid"],
    }
