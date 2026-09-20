import asyncio
import io
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Channel
from app.services import scheduler
from app.services.collector import CollectionResult


@pytest.fixture
def scheduler_sessions(monkeypatch: pytest.MonkeyPatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(scheduler, "SessionLocal", sessions)
    monkeypatch.setattr(scheduler, "_collection_batch_lock", asyncio.Lock())
    yield sessions
    engine.dispose()


@pytest.mark.asyncio
async def test_due_selection_retries_outages_without_repeated_bad_usernames(
    scheduler_sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    with scheduler_sessions() as db:
        channels = [
            Channel(username="pending", status="pending"),
            Channel(
                username="healthy_due", status="healthy",
                last_attempt_at=now - timedelta(minutes=26),
            ),
            Channel(
                username="healthy_recent", status="healthy",
                last_collected_at=now - timedelta(minutes=20),
            ),
            Channel(
                username="backfill_due", status="degraded",
                last_attempt_at=now - timedelta(minutes=26),
            ),
            Channel(
                username="new_failure", status="error",
                last_attempt_at=now - timedelta(hours=1),
            ),
            Channel(
                username="retry_first_failure", status="error",
                last_attempt_at=now - timedelta(hours=7),
            ),
            Channel(
                username="retry_known_channel", status="error",
                last_attempt_at=now - timedelta(minutes=26),
                last_collected_at=now - timedelta(days=1),
            ),
            Channel(
                username="active_collection", status="collecting",
                last_attempt_at=now - timedelta(minutes=1),
            ),
            Channel(
                username="interrupted_collection", status="collecting",
                last_attempt_at=now - timedelta(minutes=26),
            ),
        ]
        db.add_all(channels)
        db.commit()
        names_by_id = {channel.id: channel.username for channel in channels}
    calls = []

    async def fake_collect(channel_id: int) -> CollectionResult:
        calls.append(names_by_id[channel_id])
        return CollectionResult(names_by_id[channel_id], 0, 1, "healthy")

    monkeypatch.setattr(scheduler, "collect_channel_by_id", fake_collect)
    results = await scheduler.collect_due_channels(now=now)

    assert set(calls) == {
        "pending", "healthy_due", "backfill_due", "retry_first_failure",
        "retry_known_channel", "interrupted_collection",
    }
    assert len(results) == 6


@pytest.mark.asyncio
async def test_due_collection_isolates_failures_and_bounds_concurrency(
    scheduler_sessions, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with scheduler_sessions() as db:
        channels = [Channel(username=f"channel_{index}", status="pending") for index in range(6)]
        db.add_all(channels)
        db.commit()
        failed_id = channels[0].id
    active = 0
    peak = 0

    async def fake_collect(channel_id: int) -> CollectionResult:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        if channel_id == failed_id:
            raise RuntimeError("SECRET_CONNECTION_STRING")
        return CollectionResult(f"channel_{channel_id}", 0, 1, "degraded")

    monkeypatch.setattr(scheduler, "collect_channel_by_id", fake_collect)
    results = await scheduler.collect_due_channels(max_concurrency=2)

    assert peak == 2
    assert sum(result.status == "degraded" for result in results) == 5
    assert sum(result.status == "error" for result in results) == 1
    assert "SECRET_CONNECTION_STRING" not in caplog.text


@pytest.mark.asyncio
async def test_due_channels_are_rechecked_after_waiting_for_capacity(
    scheduler_sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    with scheduler_sessions() as db:
        first = Channel(username="first", status="pending")
        second = Channel(username="second", status="pending")
        db.add_all([first, second])
        db.commit()
        first_id, second_id = first.id, second.id
    calls = []

    async def fake_collect(channel_id: int) -> CollectionResult:
        calls.append(channel_id)
        with scheduler_sessions() as db:
            updated = db.get(Channel, second_id)
            updated.status = "degraded"
            updated.last_attempt_at = now
            db.commit()
        return CollectionResult("first", 0, 1, "healthy")

    monkeypatch.setattr(scheduler, "collect_channel_by_id", fake_collect)
    results = await scheduler.collect_due_channels(now=now, max_concurrency=1)

    assert calls == [first_id]
    assert len(results) == 1


@pytest.mark.asyncio
async def test_concurrent_startup_and_cron_sweeps_collect_each_channel_once(
    scheduler_sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    with scheduler_sessions() as db:
        db.add_all(Channel(username=f"channel_{index}", status="pending") for index in range(4))
        db.commit()
    calls = []
    active = 0
    peak = 0

    async def fake_collect(channel_id: int) -> CollectionResult:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        calls.append(channel_id)
        await asyncio.sleep(0)
        with scheduler_sessions() as db:
            channel = db.get(Channel, channel_id)
            channel.last_attempt_at = now
            channel.last_collected_at = now
            channel.status = "degraded"
            username = channel.username
            db.commit()
        active -= 1
        return CollectionResult(username, 0, 1, "degraded")

    monkeypatch.setattr(scheduler, "collect_channel_by_id", fake_collect)
    first, second = await asyncio.gather(
        scheduler.collect_due_channels(now=now, max_concurrency=2),
        scheduler.collect_due_channels(now=now, max_concurrency=2),
    )

    assert len(first) == 4
    assert second == []
    assert len(calls) == len(set(calls)) == 4
    assert peak == 2


@pytest.mark.asyncio
async def test_scheduler_runs_immediately_and_stops_without_another_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = asyncio.Event()

    async def collect_and_stop() -> list[CollectionResult]:
        stop.set()
        return []

    collect = AsyncMock(side_effect=collect_and_stop)
    monkeypatch.setattr(scheduler, "collect_due_channels", collect)

    await scheduler.run_collection_scheduler(stop)

    collect.assert_awaited_once()


@pytest.mark.asyncio
async def test_scheduler_continues_after_cycle_failure_without_logging_secrets(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    stop = asyncio.Event()
    collect = AsyncMock(side_effect=[RuntimeError("SECRET_DB_URL"), []])
    monkeypatch.setattr(scheduler, "collect_due_channels", collect)

    async def fake_wait(awaitable, *, timeout):
        del timeout
        awaitable.close()
        if collect.await_count == 1:
            raise TimeoutError
        stop.set()

    monkeypatch.setattr(scheduler.asyncio, "wait_for", fake_wait)

    await scheduler.run_collection_scheduler(stop)

    assert collect.await_count == 2
    assert "RuntimeError" in caplog.text
    assert "SECRET_DB_URL" not in caplog.text


@pytest.mark.asyncio
async def test_scheduler_cancels_collection_at_cycle_deadline(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    stop = asyncio.Event()
    cancelled = asyncio.Event()

    async def never_finishes() -> list[CollectionResult]:
        stop.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return []

    monkeypatch.setattr(scheduler, "collect_due_channels", never_finishes)
    monkeypatch.setattr(scheduler.asyncio, "timeout", lambda _: asyncio.timeout_at(0))

    await scheduler.run_collection_scheduler(stop)

    assert cancelled.is_set()
    assert "cycle deadline" in caplog.text


@pytest.mark.asyncio
async def test_scheduler_cancellation_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    collect = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(scheduler, "collect_due_channels", collect)

    with pytest.raises(asyncio.CancelledError):
        await scheduler.run_collection_scheduler(asyncio.Event())


@pytest.mark.parametrize(
    ("payload", "expected_code", "warning"),
    [
        (
            {"collected": 3, "healthy": 1, "backfilling": 1, "errors": ["bad"],
             "source_healthy": 2, "source_errors": ["bad"]},
            0, True,
        ),
        (
            {"collected": 1, "healthy": 0, "backfilling": 1, "errors": [],
             "source_healthy": 1, "source_errors": []},
            0, False,
        ),
        (
            {"collected": 2, "healthy": 0, "backfilling": 0, "errors": ["bad", "bad2"],
             "source_healthy": 0, "source_errors": ["bad", "bad2"]},
            1, True,
        ),
        (
            {"collected": 0, "healthy": 0, "backfilling": 0, "errors": [],
             "source_healthy": 0, "source_errors": []},
            0, False,
        ),
        (
            {"collected": 0, "healthy": 0, "backfilling": 0, "errors": [],
             "source_healthy": 0, "source_errors": ["startup_failed"]},
            1, False,
        ),
        (
            {"collected": 1, "healthy": 0, "backfilling": 0, "errors": ["known_bad"],
             "source_healthy": 2, "source_errors": ["known_bad"]},
            0, True,
        ),
        (
            {"collected": 0, "healthy": 0, "backfilling": 0, "errors": [],
             "source_healthy": 2, "source_errors": ["known_bad"]},
            0, False,
        ),
    ],
)
def test_workflow_checks_persisted_health_after_startup_or_partial_failures(
    payload: dict, expected_code: int, warning: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow = Path(__file__).parents[1] / ".github" / "workflows" / "collect.yml"
    lines = workflow.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if "<<'PY'" in line) + 1
    end = next(index for index in range(start, len(lines)) if lines[index].strip() == "PY")
    source = "\n".join(line[10:] for line in lines[start:end])
    code = 0
    with (
        patch("builtins.open", return_value=io.StringIO(json.dumps(payload))),
        patch.object(sys, "argv", ["workflow_validator", "response.json"]),
    ):
        try:
            exec(compile(source, str(workflow), "exec"), {})
        except SystemExit as exc:
            code = exc.code

    assert code == expected_code
    output = capsys.readouterr().out
    assert ("::warning::" in output) is warning
    assert ("::error::" in output) is (expected_code != 0)
