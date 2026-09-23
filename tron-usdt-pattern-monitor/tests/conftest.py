"""Shared fixtures.

By default tests run on SQLite.  Set TEST_DATABASE_URL to a PostgreSQL URL
(e.g. postgresql+asyncpg://postgres@localhost/tron_test) to run the same suite
against PostgreSQL; every test starts from an empty schema.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from app.clock import ManualClock
from app.config.settings import Settings
from app.database.models import Base
from app.main import Application
from app.simulation.source import SimulatedEventSource
from app.telegram.alerts import RecordingSink
from tests.helpers import Harness

TEST_DB = os.environ.get("TEST_DATABASE_URL")


def make_settings(db_url: str, **kw) -> Settings:
    base = dict(
        database_url=db_url,
        initial_history_days=0,
        backfill_window_seconds=21600,
        send_startup_message=False,
        telegram_bot_token="",
        telegram_chat_id="",
        heartbeat_file="/tmp/tron-usdt-test.heartbeat",
        alert_retry_base_seconds=0.01,
        alert_retry_max_seconds=0.05,
        tron_retry_base_seconds=0.01,
        tron_retry_max_seconds=0.02,
    )
    base.update(kw)
    return Settings(_env_file=None, **base)


@pytest.fixture
def db_url(tmp_path):
    return TEST_DB or f"sqlite+aiosqlite:///{tmp_path}/test.db"


@pytest.fixture
async def make_harness(db_url):
    created: list[Harness] = []
    reset_done = False

    async def factory(*, clock: ManualClock | None = None, sink: RecordingSink | None = None, fresh: bool = True, **kw):
        nonlocal reset_done
        settings = make_settings(db_url, **kw)
        clock = clock or ManualClock(datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc))
        src = SimulatedEventSource()
        rec = sink or RecordingSink()
        app = Application(settings, source=src, sink=rec, clock=clock)
        if db_url.startswith("postgresql") and fresh and not reset_done:
            async with app.engine.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
                await conn.run_sync(Base.metadata.create_all)
            reset_done = True
        if db_url.startswith("postgresql"):
            settings.auto_migrate = False
        await app.start()
        h = Harness(app, src, rec, clock)
        created.append(h)
        return h

    yield factory
    for h in created:
        try:
            await h.app.close()
        except Exception:
            pass


@pytest.fixture
def t0():
    return datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc) - timedelta(days=10)
