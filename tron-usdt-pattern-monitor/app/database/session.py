"""Engine / session factory and schema bootstrap."""

from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database.models import Base
from app.logging_setup import get_logger

log = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def create_engine(url: str, pool_size: int = 10) -> AsyncEngine:
    if url.startswith("sqlite"):
        kwargs: dict = {"connect_args": {"timeout": 30}}
        if ":memory:" in url or url.endswith("sqlite+aiosqlite://"):
            kwargs["poolclass"] = StaticPool
        engine = create_async_engine(url, **kwargs)

        @event.listens_for(engine.sync_engine, "connect")
        def _pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

        return engine
    return create_async_engine(
        url,
        pool_size=pool_size,
        max_overflow=pool_size,
        pool_pre_ping=True,
        pool_recycle=1800,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


def is_transient_db_error(exc: BaseException) -> bool:
    """Connection / availability problems that should be retried, not skipped."""
    if isinstance(exc, (OperationalError, InterfaceError, ConnectionError, OSError, asyncio.TimeoutError)):
        return True
    if isinstance(exc, DBAPIError) and exc.connection_invalidated:
        return True
    name = type(exc).__name__
    return name in {"ConnectionDoesNotExistError", "CannotConnectNowError", "TooManyConnectionsError"}


async def wait_for_database(engine: AsyncEngine, attempts: int = 60) -> None:
    delay = 1.0
    for i in range(1, attempts + 1):
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("Database not reachable yet", attempt=i, error=type(exc).__name__)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
    raise RuntimeError("database unreachable")


async def create_all(engine: AsyncEngine) -> None:
    """Create tables directly from the models (SQLite tests / simulation)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def run_alembic_upgrade(database_url: str) -> None:
    """Apply Alembic migrations (blocking; run in a worker thread)."""
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    command.upgrade(cfg, "head")


async def init_schema(engine: AsyncEngine, database_url: str, auto_migrate: bool) -> None:
    if database_url.startswith("sqlite"):
        await create_all(engine)
        return
    if auto_migrate:
        log.info("Applying database migrations")
        await asyncio.to_thread(run_alembic_upgrade, database_url)
