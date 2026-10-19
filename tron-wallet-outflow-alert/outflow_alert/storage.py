"""SQLite persistence: scan cursor, verification queue, verified transfers and Telegram outbox.

Everything that must survive a restart lives here:

* ``kv.cursor_ms``    - newest block timestamp fully scanned (scans re-read a lookback window before it)
* ``candidates``      - amount-matching txids waiting for on-chain verification
* ``verified_txs``    - txids already verified (alerted / rejected / expired); never re-processed
* ``transfers``       - every verified matching Transfer log (one row per txid+log index)
* ``outbox``          - one row per (transfer, chat); deleted only after Telegram accepted it
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates (
    txid            TEXT PRIMARY KEY,
    block_ts        INTEGER NOT NULL,
    first_seen      INTEGER NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER NOT NULL,
    last_error      TEXT
);
CREATE TABLE IF NOT EXISTS verified_txs (
    txid        TEXT PRIMARY KEY,
    outcome     TEXT NOT NULL,          -- alerted | rejected | expired
    reason      TEXT,
    verified_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS transfers (
    txid         TEXT NOT NULL,
    log_index    INTEGER NOT NULL,
    from_address TEXT NOT NULL,
    to_address   TEXT NOT NULL,
    amount_raw   TEXT NOT NULL,         -- integer base units, stored as text (no 64-bit limit)
    target_raw   TEXT NOT NULL,
    block_number INTEGER,
    block_ts     INTEGER,
    created_at   INTEGER NOT NULL,
    PRIMARY KEY (txid, log_index)
);
CREATE TABLE IF NOT EXISTS outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    txid            TEXT NOT NULL,
    log_index       INTEGER NOT NULL,
    chat_id         TEXT NOT NULL,
    text            TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER NOT NULL,
    last_error      TEXT,
    UNIQUE (txid, log_index, chat_id)
);
CREATE INDEX IF NOT EXISTS ix_candidates_due ON candidates (next_attempt_at);
CREATE INDEX IF NOT EXISTS ix_outbox_due ON outbox (next_attempt_at);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class Candidate:
    txid: str
    block_ts: int
    first_seen: int
    attempts: int


@dataclass(frozen=True)
class OutboxItem:
    id: int
    txid: str
    log_index: int
    chat_id: str
    text: str
    attempts: int


@dataclass(frozen=True)
class MatchedTransfer:
    txid: str
    log_index: int
    from_address: str
    to_address: str
    amount_raw: int
    target_raw: int
    block_number: int | None
    block_ts: int | None


class Storage:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, isolation_level=None, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    # --------------------------------------------------------------------- kv
    def get(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def get_cursor(self) -> int | None:
        value = self.get("cursor_ms")
        return int(value) if value is not None else None

    def advance_cursor(self, ts_ms: int) -> None:
        """Move the cursor forward only (never backwards)."""
        current = self.get_cursor()
        if current is None or ts_ms > current:
            self.set("cursor_ms", str(int(ts_ms)))

    # ------------------------------------------------------------- candidates
    def is_known(self, txid: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM verified_txs WHERE txid = ? UNION ALL SELECT 1 FROM candidates WHERE txid = ? LIMIT 1",
            (txid, txid),
        ).fetchone()
        return row is not None

    def add_candidate(self, txid: str, block_ts: int) -> bool:
        if self.is_known(txid):
            return False
        ts = now_ms()
        cur = self._db.execute(
            "INSERT OR IGNORE INTO candidates (txid, block_ts, first_seen, next_attempt_at) VALUES (?, ?, ?, ?)",
            (txid, int(block_ts), ts, ts),
        )
        return cur.rowcount == 1

    def due_candidates(self, limit: int = 50) -> list[Candidate]:
        rows = self._db.execute(
            "SELECT txid, block_ts, first_seen, attempts FROM candidates WHERE next_attempt_at <= ? "
            "ORDER BY block_ts, txid LIMIT ?",
            (now_ms(), limit),
        ).fetchall()
        return [Candidate(r["txid"], r["block_ts"], r["first_seen"], r["attempts"]) for r in rows]

    def reschedule_candidate(self, txid: str, delay_ms: int, error: str) -> None:
        self._db.execute(
            "UPDATE candidates SET attempts = attempts + 1, next_attempt_at = ?, last_error = ? WHERE txid = ?",
            (now_ms() + int(delay_ms), error[:500], txid),
        )

    def pending_count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0])

    def finish_candidate(
        self,
        txid: str,
        outcome: str,
        reason: str | None,
        matches: Iterable[MatchedTransfer] = (),
        alerts: Iterable[tuple[MatchedTransfer, str, str]] = (),
    ) -> int:
        """Atomically record the verification result, matching transfers and their outbox rows.

        ``alerts`` is an iterable of ``(transfer, chat_id, text)``.  Returns number of outbox rows created.
        """
        ts = now_ms()
        created = 0
        self._db.execute("BEGIN IMMEDIATE")
        try:
            for m in matches:
                self._db.execute(
                    "INSERT OR IGNORE INTO transfers (txid, log_index, from_address, to_address, amount_raw, "
                    "target_raw, block_number, block_ts, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (m.txid, m.log_index, m.from_address, m.to_address, str(m.amount_raw), str(m.target_raw),
                     m.block_number, m.block_ts, ts),
                )
            for m, chat_id, text in alerts:
                cur = self._db.execute(
                    "INSERT OR IGNORE INTO outbox (txid, log_index, chat_id, text, created_at, next_attempt_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (m.txid, m.log_index, chat_id, text, ts, ts),
                )
                created += cur.rowcount
            self._db.execute(
                "INSERT OR REPLACE INTO verified_txs (txid, outcome, reason, verified_at) VALUES (?, ?, ?, ?)",
                (txid, outcome, reason, ts),
            )
            self._db.execute("DELETE FROM candidates WHERE txid = ?", (txid,))
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        return created

    def verified_outcome(self, txid: str) -> str | None:
        row = self._db.execute("SELECT outcome FROM verified_txs WHERE txid = ?", (txid,)).fetchone()
        return row["outcome"] if row else None

    def transfer_count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM transfers").fetchone()[0])

    # ------------------------------------------------------------------ outbox
    def due_outbox(self, limit: int = 20) -> list[OutboxItem]:
        rows = self._db.execute(
            "SELECT id, txid, log_index, chat_id, text, attempts FROM outbox WHERE next_attempt_at <= ? "
            "ORDER BY id LIMIT ?",
            (now_ms(), limit),
        ).fetchall()
        return [OutboxItem(r["id"], r["txid"], r["log_index"], r["chat_id"], r["text"], r["attempts"]) for r in rows]

    def outbox_sent(self, item_id: int) -> None:
        self._db.execute("DELETE FROM outbox WHERE id = ?", (item_id,))

    def outbox_retry(self, item_id: int, delay_ms: int, error: str) -> None:
        self._db.execute(
            "UPDATE outbox SET attempts = attempts + 1, next_attempt_at = ?, last_error = ? WHERE id = ?",
            (now_ms() + int(delay_ms), error[:500], item_id),
        )

    def outbox_count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])

    def next_outbox_due_ms(self) -> int | None:
        row = self._db.execute("SELECT MIN(next_attempt_at) FROM outbox").fetchone()
        return int(row[0]) if row and row[0] is not None else None
