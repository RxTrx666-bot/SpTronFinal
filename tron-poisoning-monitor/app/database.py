"""Database engine, sessions, migrations and retry helpers.

Production uses PostgreSQL with plain-SQL migrations from ``migrations/*.sql``
applied in order and recorded in ``schema_migrations`` (each file runs in its
own transaction, so a crash mid-migration leaves the schema unchanged).
SQLite (tests / simulation) uses ``Base.metadata.create_all``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar

from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.utils.logging import get_logger

log = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
T = TypeVar("T")


def create_engine(url: str, pool_size: int = 10) -> AsyncEngine:
    if url.startswith("sqlite"):
        kwargs: dict = {"connect_args": {"timeout": 30}}
        if ":memory:" in url or url.rstrip("/").endswith("sqlite+aiosqlite:"):
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
    return create_async_engine(url, pool_size=pool_size, max_overflow=pool_size, pool_pre_ping=True, pool_recycle=1800)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


def is_transient_db_error(exc: BaseException) -> bool:
    """Connection/availability problems that must be retried (never skipped)."""
    if isinstance(exc, (OperationalError, InterfaceError, ConnectionError, OSError, asyncio.TimeoutError)):
        return True
    if isinstance(exc, DBAPIError) and exc.connection_invalidated:
        return True
    name = type(exc).__name__
    return name in {"ConnectionDoesNotExistError", "CannotConnectNowError", "TooManyConnectionsError", "AdminShutdownError"}


async def with_db_retry(fn: Callable[[], Awaitable[T]], *, what: str, max_delay: float = 30.0, attempts: int = 0) -> T:
    """Run ``fn`` retrying transient DB errors with exponential backoff (attempts=0: forever)."""
    delay = 0.5
    n = 0
    while True:
        n += 1
        try:
            return await fn()
        except Exception as exc:
            if not is_transient_db_error(exc) or (attempts and n >= attempts):
                raise
            log.warning("DB_UNAVAILABLE", operation=what, attempt=n, retry_in=f"{delay:.1f}s", error=type(exc).__name__)
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)


async def wait_for_database(engine: AsyncEngine, attempts: int = 60) -> None:
    delay = 1.0
    for i in range(1, attempts + 1):
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("DB_WAIT", attempt=i, error=type(exc).__name__)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15)
    raise RuntimeError("database unreachable")


def migration_files() -> list[Path]:
    return sorted(p for p in MIGRATIONS_DIR.glob("*.sql"))


def _split_sql(sql: str) -> list[str]:
    """Split a migration into statements (no dollar-quoted bodies are used in our migrations)."""
    out, buf = [], []
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--") or not stripped:
            continue
        buf.append(line)
        if stripped.endswith(";"):
            out.append("\n".join(buf).rstrip().rstrip(";"))
            buf = []
    if buf:
        out.append("\n".join(buf))
    return out


async def apply_migrations(engine: AsyncEngine) -> list[str]:
    """Apply pending PostgreSQL migrations.  Serialised with an advisory lock."""
    applied_now: list[str] = []
    async with engine.connect() as conn:
        await conn.execute(text("SELECT pg_advisory_lock(727274)"))  # session-level: survives commits
        await conn.commit()
        try:
            await conn.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"))
            await conn.commit()
            done = {r[0] for r in (await conn.execute(text("SELECT version FROM schema_migrations"))).all()}
            await conn.commit()
            for path in migration_files():
                if path.name in done:
                    continue
                async with conn.begin():
                    for stmt in _split_sql(path.read_text()):
                        await conn.execute(text(stmt))
                    await conn.execute(text("INSERT INTO schema_migrations(version) VALUES (:v)"), {"v": path.name})
                applied_now.append(path.name)
                log.info("MIGRATION_APPLIED", version=path.name)
        finally:
            await conn.execute(text("SELECT pg_advisory_unlock(727274)"))
            await conn.commit()
    return applied_now


async def init_schema(engine: AsyncEngine, url: str, auto_migrate: bool) -> None:
    if url.startswith("sqlite"):
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        return
    if auto_migrate:
        await apply_migrations(engine)
