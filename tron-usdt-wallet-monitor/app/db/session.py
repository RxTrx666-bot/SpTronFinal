"""Engine / session factory, connection-failure classification and migrations."""

from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.logging_setup import get_logger

log = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def create_engine(url: str, pool_size: int = 10) -> AsyncEngine:
    return create_async_engine(url, pool_size=pool_size, max_overflow=pool_size, pool_pre_ping=True, pool_recycle=1800)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


def is_transient_db_error(exc: BaseException) -> bool:
    """Connection/availability problems: retry, never skip data."""
    if isinstance(exc, (OperationalError, InterfaceError, ConnectionError, OSError, asyncio.TimeoutError)):
        return True
    if isinstance(exc, DBAPIError) and exc.connection_invalidated:
        return True
    return type(exc).__name__ in {"ConnectionDoesNotExistError", "CannotConnectNowError", "TooManyConnectionsError"}


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


def run_migrations(database_url: str) -> None:
    """``alembic upgrade head`` (blocking; call via ``asyncio.to_thread``)."""
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    command.upgrade(cfg, "head")
