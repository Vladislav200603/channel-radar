import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.pool import NullPool

from app import models  # noqa: F401 - populate the current ORM metadata
from app.database import Base

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# subprocesses do not inherit pytest-socket's patches: enforce the same no-network
# boundary there before importing Alembic or any application settings.
ALEMBIC_RUNNER = (
    "from pytest_socket import disable_socket; disable_socket(); from alembic.config import main; main()"
)


def run_alembic(database_url: str, *arguments: str) -> str:
    environment = os.environ.copy()
    environment.update(
        DATABASE_URL=database_url,
        GEMINI_API_KEY="",
        CRON_SECRET="",
        SCHEDULER_ENABLED="false",
        PYTHONIOENCODING="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-c", ALEMBIC_RUNNER, *arguments],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=30,
    )
    return result.stdout


def reflect_tables(engine: sa.Engine) -> sa.MetaData:
    metadata = sa.MetaData()
    metadata.reflect(bind=engine)
    return metadata


def test_legacy_collection_migration_preserves_data_and_round_trips(tmp_path: Path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'legacy-migration.db').as_posix()}"
    run_alembic(database_url, "upgrade", "0002")
    engine = sa.create_engine(database_url, poolclass=NullPool)
    legacy = reflect_tables(engine)
    now = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
    with engine.begin() as connection:
        connection.execute(
            legacy.tables["channels"].insert(),
            [
                {
                    "id": 1,
                    "username": "example",
                    "title": "Legacy title",
                    "status": "degraded",
                    "subscriber_count": 100,
                    "created_at": now,
                    "updated_at": now,
                    "last_attempt_at": now,
                    "last_collected_at": now,
                    "backfill_cursor": "/s/example?before=42",
                },
                {
                    "id": 2,
                    "username": "another",
                    "title": "Second title",
                    "status": "healthy",
                    "subscriber_count": 200,
                    "created_at": now,
                    "updated_at": now,
                    "last_attempt_at": now,
                    "last_collected_at": now,
                    "backfill_cursor": None,
                },
            ],
        )
        connection.execute(
            legacy.tables["posts"]
            .insert()
            .values(
                id=10,
                channel_id=1,
                telegram_post_id=101,
                url="https://t.me/example/101",
                text="Existing post must survive the migration",
                published_at=now,
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        connection.execute(
            legacy.tables["channel_metric_snapshots"]
            .insert()
            .values(
                id=20,
                channel_id=1,
                observed_bucket=now,
                subscriber_count=100,
                posts_seen=1,
                collected_at=now,
            )
        )
        connection.execute(
            legacy.tables["post_metric_snapshots"]
            .insert()
            .values(
                id=30,
                post_id=10,
                observed_bucket=now,
                views=123,
                reactions_total=None,
                reactions={},
                collected_at=now,
            )
        )
        connection.execute(
            legacy.tables["channel_digests"]
            .insert()
            .values(
                id=40,
                channel_id=1,
                period_start=now - timedelta(days=7),
                period_end=now,
                content="Existing saved digest",
                provider="google",
                model="legacy-model",
                created_at=now,
            )
        )

    run_alembic(database_url, "upgrade", "0003")
    upgraded = reflect_tables(engine)
    with engine.connect() as connection:
        channels = (
            connection.execute(
                sa.select(upgraded.tables["channels"]).order_by(upgraded.tables["channels"].c.id)
            )
            .mappings()
            .all()
        )
        assert [channel["title"] for channel in channels] == ["Legacy title", "Second title"]
        assert [channel["status"] for channel in channels] == ["degraded", "healthy"]
        for channel in channels:
            assert channel["backfill_cursor"] is None  # Legacy cursor had ambiguous meaning.
            assert channel["backfill_stop_id"] is None
            assert channel["pending_gaps"] == []
            assert channel["archive_initialized"] is False  # Enrol even channels without an old cursor.
            assert channel["archive_cursor"] is None
            assert channel["archive_error"] is None
        snapshot = connection.execute(sa.select(upgraded.tables["channel_metric_snapshots"])).mappings().one()
        assert snapshot["subscriber_count"] == 100
        assert snapshot["fresh_average_views"] is None  # Never fabricate a historical fresh sample.
        assert upgraded.tables["channel_metric_snapshots"].c.fresh_average_views.nullable
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []

    run_alembic(database_url, "downgrade", "0002")
    downgraded = reflect_tables(engine)
    assert "archive_initialized" not in downgraded.tables["channels"].c
    assert "fresh_average_views" not in downgraded.tables["channel_metric_snapshots"].c
    run_alembic(database_url, "upgrade", "head")
    current = reflect_tables(engine)
    with engine.connect() as connection:
        assert connection.scalar(sa.select(current.tables["posts"].c.text)) == (
            "Existing post must survive the migration"
        )
        assert connection.scalar(sa.select(current.tables["posts"].c.telegram_post_id)) == 101
        assert connection.scalar(sa.select(current.tables["post_metric_snapshots"].c.views)) == 123
        assert connection.scalar(sa.select(current.tables["post_metric_snapshots"].c.reactions_total)) is None
        assert (
            connection.scalar(sa.select(current.tables["channel_digests"].c.content))
            == "Existing saved digest"
        )
        assert connection.scalar(sa.select(current.tables["alembic_version"].c.version_num)) == "0003"
    engine.dispose()


def test_postgres_upgrade_and_downgrade_generate_offline_without_connections() -> None:
    database_url = "postgresql+psycopg://offline:offline@127.0.0.1:9/offline"
    upgrade_sql = run_alembic(database_url, "upgrade", "0002:0003", "--sql")
    assert "ALTER TABLE channels ADD COLUMN pending_gaps JSON DEFAULT '[]' NOT NULL" in upgrade_sql
    assert "ALTER TABLE channels ADD COLUMN archive_initialized BOOLEAN DEFAULT false NOT NULL" in upgrade_sql
    assert "ALTER TABLE channel_metric_snapshots ADD COLUMN fresh_average_views FLOAT" in upgrade_sql
    assert "UPDATE channels SET backfill_cursor = NULL" in upgrade_sql
    assert "COMMIT;" in upgrade_sql
    downgrade_sql = run_alembic(database_url, "downgrade", "0003:0002", "--sql")
    assert "ALTER TABLE channels DROP COLUMN archive_initialized" in downgrade_sql
    assert "ALTER TABLE channel_metric_snapshots DROP COLUMN fresh_average_views" in downgrade_sql
    assert "COMMIT;" in downgrade_sql
