"""Persistence layer.

``Repository`` is the storage interface used by the rest of the app; ``SQLiteRepository``
is the initial implementation. To move to PostgreSQL, implement ``Repository`` (e.g. with
asyncpg, ``INSERT ... ON CONFLICT (tx_hash) DO NOTHING``) and return it from
``create_repository`` for ``postgresql://`` URLs. Nothing else needs to change.
"""

from __future__ import annotations

import abc
import asyncio
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.timeutil import iso_utc, now_ms

ALERT_PENDING = "pending"
ALERT_SENT = "sent"


@dataclass(frozen=True)
class NewTransaction:
    tx_hash: str
    block_number: int | None
    block_timestamp_ms: int
    direction: str
    sender: str
    recipient: str
    amount_raw: int
    amount_usdt: str  # exact decimal string, e.g. "1.100000"
    contract_address: str
    token_symbol: str
    detected_at_ms: int
    is_backfill: bool = False
    source: str = ""


@dataclass(frozen=True)
class StoredTransaction:
    id: int
    tx_hash: str
    block_number: int | None
    block_timestamp_ms: int
    timestamp: str
    direction: str
    sender: str
    recipient: str
    amount_raw: int
    amount_usdt: str
    contract_address: str
    token_symbol: str
    detected_at_ms: int
    is_backfill: bool
    source: str
    alert_status: str
    alerted_at_ms: int | None
    alert_attempts: int
    created_at: str


class Repository(abc.ABC):
    @abc.abstractmethod
    async def init(self) -> None: ...

    @abc.abstractmethod
    async def close(self) -> None: ...

    @abc.abstractmethod
    async def exists(self, tx_hash: str) -> bool: ...

    @abc.abstractmethod
    async def insert_transaction(self, tx: NewTransaction) -> bool:
        """Atomically insert; returns False if tx_hash already exists (duplicate)."""

    @abc.abstractmethod
    async def get_transaction(self, tx_hash: str) -> StoredTransaction | None: ...

    @abc.abstractmethod
    async def mark_alert_sent(self, tx_hash: str, alerted_at_ms: int) -> None: ...

    @abc.abstractmethod
    async def increment_alert_attempts(self, tx_hash: str) -> None: ...

    @abc.abstractmethod
    async def pending_alerts(self) -> list[StoredTransaction]: ...

    @abc.abstractmethod
    async def count_transactions(self) -> int: ...

    @abc.abstractmethod
    async def latest_transaction(self) -> StoredTransaction | None: ...

    @abc.abstractmethod
    async def get_state(self, key: str) -> str | None: ...

    @abc.abstractmethod
    async def set_state(self, key: str, value: str) -> None: ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    tx_hash            TEXT    NOT NULL,
    block_number       INTEGER,
    timestamp          TEXT    NOT NULL,          -- blockchain time, ISO-8601 UTC
    block_timestamp_ms INTEGER NOT NULL,          -- blockchain time, epoch ms
    direction          TEXT    NOT NULL,          -- INCOMING | OUTGOING | SELF
    sender             TEXT    NOT NULL,
    recipient          TEXT    NOT NULL,
    amount_usdt        TEXT    NOT NULL,          -- exact decimal string (no float)
    amount_raw         INTEGER NOT NULL,          -- integer base units (6 decimals)
    contract_address   TEXT    NOT NULL,
    token_symbol       TEXT    NOT NULL,
    source             TEXT    NOT NULL DEFAULT '',
    is_backfill        INTEGER NOT NULL DEFAULT 0,
    detected_at_ms     INTEGER NOT NULL,
    alert_status       TEXT    NOT NULL DEFAULT 'pending',
    alerted_at_ms      INTEGER,
    alert_attempts     INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_transactions_tx_hash ON transactions (tx_hash);
CREATE INDEX IF NOT EXISTS ix_transactions_alert_status ON transactions (alert_status);
CREATE INDEX IF NOT EXISTS ix_transactions_block_ts ON transactions (block_timestamp_ms);

CREATE TABLE IF NOT EXISTS monitor_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_COLUMNS = (
    "id, tx_hash, block_number, block_timestamp_ms, timestamp, direction, sender, recipient, "
    "amount_raw, amount_usdt, contract_address, token_symbol, detected_at_ms, is_backfill, "
    "source, alert_status, alerted_at_ms, alert_attempts, created_at"
)


def _row_to_tx(row: tuple[Any, ...] | None) -> StoredTransaction | None:
    if row is None:
        return None
    values = list(row)
    values[13] = bool(values[13])
    return StoredTransaction(*values)


class SQLiteRepository(Repository):
    """SQLite (WAL mode) repository. Blocking calls are moved off the event loop."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def _connect(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.executescript(_SCHEMA)
        conn.execute("PRAGMA user_version=1")
        self._conn = conn

    async def _run(self, fn, *args):
        def locked():
            with self._lock:
                if self._conn is None:
                    raise RuntimeError("repository not initialised")
                return fn(self._conn, *args)

        return await asyncio.to_thread(locked)

    async def init(self) -> None:
        await asyncio.to_thread(self._connect)

    async def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    async def exists(self, tx_hash: str) -> bool:
        def q(c: sqlite3.Connection) -> bool:
            return c.execute("SELECT 1 FROM transactions WHERE tx_hash = ?", (tx_hash,)).fetchone() is not None

        return await self._run(q)

    async def insert_transaction(self, tx: NewTransaction) -> bool:
        def q(c: sqlite3.Connection) -> bool:
            cur = c.execute(
                """
                INSERT INTO transactions (
                    tx_hash, block_number, timestamp, block_timestamp_ms, direction, sender,
                    recipient, amount_usdt, amount_raw, contract_address, token_symbol, source,
                    is_backfill, detected_at_ms, alert_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tx_hash) DO NOTHING
                """,
                (
                    tx.tx_hash, tx.block_number, iso_utc(tx.block_timestamp_ms), tx.block_timestamp_ms,
                    tx.direction, tx.sender, tx.recipient, tx.amount_usdt, tx.amount_raw,
                    tx.contract_address, tx.token_symbol, tx.source, int(tx.is_backfill),
                    tx.detected_at_ms, ALERT_PENDING, iso_utc(now_ms()),
                ),
            )
            return cur.rowcount == 1

        return await self._run(q)

    async def get_transaction(self, tx_hash: str) -> StoredTransaction | None:
        def q(c: sqlite3.Connection):
            return c.execute(f"SELECT {_COLUMNS} FROM transactions WHERE tx_hash = ?", (tx_hash,)).fetchone()

        return _row_to_tx(await self._run(q))

    async def mark_alert_sent(self, tx_hash: str, alerted_at_ms: int) -> None:
        def q(c: sqlite3.Connection) -> None:
            c.execute(
                "UPDATE transactions SET alert_status = ?, alerted_at_ms = ?, "
                "alert_attempts = alert_attempts + 1 WHERE tx_hash = ?",
                (ALERT_SENT, alerted_at_ms, tx_hash),
            )

        await self._run(q)

    async def increment_alert_attempts(self, tx_hash: str) -> None:
        def q(c: sqlite3.Connection) -> None:
            c.execute("UPDATE transactions SET alert_attempts = alert_attempts + 1 WHERE tx_hash = ?", (tx_hash,))

        await self._run(q)

    async def pending_alerts(self) -> list[StoredTransaction]:
        def q(c: sqlite3.Connection):
            return c.execute(
                f"SELECT {_COLUMNS} FROM transactions WHERE alert_status = ? ORDER BY block_timestamp_ms, id",
                (ALERT_PENDING,),
            ).fetchall()

        return [_row_to_tx(r) for r in await self._run(q)]  # type: ignore[misc]

    async def count_transactions(self) -> int:
        def q(c: sqlite3.Connection) -> int:
            return int(c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])

        return await self._run(q)

    async def latest_transaction(self) -> StoredTransaction | None:
        def q(c: sqlite3.Connection):
            return c.execute(
                f"SELECT {_COLUMNS} FROM transactions ORDER BY block_timestamp_ms DESC, id DESC LIMIT 1"
            ).fetchone()

        return _row_to_tx(await self._run(q))

    async def get_state(self, key: str) -> str | None:
        def q(c: sqlite3.Connection):
            row = c.execute("SELECT value FROM monitor_state WHERE key = ?", (key,)).fetchone()
            return row[0] if row else None

        return await self._run(q)

    async def set_state(self, key: str, value: str) -> None:
        def q(c: sqlite3.Connection) -> None:
            c.execute(
                "INSERT INTO monitor_state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, value, iso_utc(now_ms())),
            )

        await self._run(q)


def create_repository(database_url: str) -> Repository:
    if database_url.startswith("sqlite:///"):
        return SQLiteRepository(database_url[len("sqlite:///"):])
    if database_url == "sqlite://:memory:":
        return SQLiteRepository(":memory:")
    if database_url.startswith(("postgres://", "postgresql://")):
        raise NotImplementedError(
            "PostgreSQL support: implement app.database.Repository for PostgreSQL and register it here"
        )
    raise ValueError("DATABASE_URL must look like sqlite:///path/to/monitor.db")
