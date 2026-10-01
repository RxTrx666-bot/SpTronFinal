"""Tests run against a real PostgreSQL database (the production engine).

Set TEST_DATABASE_URL (default: postgresql+asyncpg://tron:tron@localhost:5432/tron_usdt_test).
The schema is created through the Alembic migration once per session and every
table is truncated before each test.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings
from app.db.session import create_session_factory, run_migrations
from app.main import build_runtime
from tests.fakes import T0, USDT, WALLET_A, FakeTron

TEST_DB = os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://tron:tron@localhost:5432/tron_usdt_test")


@pytest.fixture(scope="session")
def migrated_db():
    import asyncio

    async def reset():
        eng = create_async_engine(TEST_DB)
        try:
            async with eng.begin() as conn:
                await conn.execute(text("DROP SCHEMA public CASCADE"))
                await conn.execute(text("CREATE SCHEMA public"))
        finally:
            await eng.dispose()

    try:
        asyncio.run(reset())
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL not reachable at TEST_DATABASE_URL: {exc}")
    run_migrations(TEST_DB)
    return TEST_DB


@pytest.fixture
async def engine(migrated_db):
    eng = create_async_engine(migrated_db)
    async with eng.begin() as conn:
        await conn.execute(text("TRUNCATE wallets, transfers, alerts, checkpoints, system_state RESTART IDENTITY"))
    yield eng
    await eng.dispose()


@pytest.fixture
def sf(engine):
    return create_session_factory(engine)


def make_settings(**overrides) -> Settings:
    base = dict(
        root_wallet=WALLET_A,
        usdt_contract=USDT,
        telegram_bot_token="123456:TEST-TOKEN-abcdef",
        telegram_admin_chat_id="1000000001",
        database_url=TEST_DB,
        tron_api_key="test-api-key-000000",
        alert_min_amount_usdt="500",
        stream_overlap_seconds=30,
        reconcile_max_requests_per_second=1000,  # no background throttling in tests
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)


@pytest.fixture
def settings():
    return make_settings()


@pytest.fixture
def tron():
    return FakeTron()


@pytest.fixture
async def rt(settings, sf, tron):
    """A runtime whose monitoring started at T0."""
    return await build_runtime(settings, sf, tron, now_ms=T0)
