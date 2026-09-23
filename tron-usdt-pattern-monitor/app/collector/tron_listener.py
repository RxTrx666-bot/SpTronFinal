"""Centralised, READ-ONLY TRON USDT event collector.

ONE collector reads every USDT TRC-20 ``Transfer`` event from the USDT contract
event stream (TronGrid ``/v1/contracts/{contract}/events``) and hands the
decoded transfers to the pipeline.  There is no per-wallet polling.

Three loops share the same source:

* confirmed loop   - incremental polling from a persisted cursor
                     (``collector_state.confirmed_cursor_ms``) with a small
                     overlap; restarts resume exactly where they stopped.
* unconfirmed loop - optional fast path for not-yet-solidified events, used
                     only for early alerting (stored as ``UNCONFIRMED``).
* backfill         - one-time historical scan of INITIAL_HISTORY_DAYS in time
                     windows, resumable via ``collector_state.backfill_next_ms``.

This module only performs HTTP GET/POST *read* calls.  It never signs,
broadcasts, or builds transactions, and never handles keys.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.clock import Clock
from app.collector.event_parser import parse_events
from app.config.settings import Settings
from app.database import repository as repo
from app.database.session import is_transient_db_error
from app.domain import datetime_to_ms, ms_to_datetime
from app.logging_setup import get_logger

log = get_logger(__name__)

STATE_CONFIRMED_CURSOR = "confirmed_cursor_ms"
STATE_BACKFILL_START = "backfill_start_ms"
STATE_BACKFILL_END = "backfill_end_ms"
STATE_BACKFILL_NEXT = "backfill_next_ms"
STATE_BACKFILL_DONE = "backfill_complete"
STATE_BACKFILL_ANALYSIS_DONE = "backfill_analysis_complete"


class TronApiError(Exception):
    def __init__(self, message: str, *, retryable: bool = True, status: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class EventSource(Protocol):
    async def get_contract_events(
        self,
        *,
        min_timestamp_ms: int | None = None,
        max_timestamp_ms: int | None = None,
        fingerprint: str | None = None,
        only_confirmed: bool = False,
        only_unconfirmed: bool = False,
        limit: int = 200,
    ) -> tuple[list[dict[str, Any]], str | None]: ...

    async def close(self) -> None: ...


class TronGridClient:
    """Minimal async TronGrid client with timeouts, retries and backoff."""

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.s = settings
        headers = {"Accept": "application/json", "User-Agent": "tron-usdt-pattern-monitor/1.0"}
        if settings.tron_api_key:
            headers["TRON-PRO-API-KEY"] = settings.tron_api_key
        self._client = httpx.AsyncClient(
            base_url=settings.tron_api_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(settings.tron_request_timeout_seconds),
            transport=transport,
        )
        self.requests = 0
        self.failures = 0

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        attempt = 0
        while True:
            attempt += 1
            try:
                self.requests += 1
                resp = await self._client.request(method, path, **kwargs)
                if resp.status_code == 429 or resp.status_code >= 500:
                    retry_after = _retry_after(resp)
                    if retry_after is not None:
                        raise _RetryAfter(retry_after, resp.status_code)
                    raise TronApiError(f"HTTP {resp.status_code}", retryable=True, status=resp.status_code)
                if resp.status_code >= 400:
                    raise TronApiError(f"HTTP {resp.status_code}: {resp.text[:200]}", retryable=False, status=resp.status_code)
                data = resp.json()
                if isinstance(data, dict) and data.get("success") is False:
                    raise TronApiError(f"API error: {str(data.get('error'))[:200]}", retryable=True)
                return data
            except (_RetryAfter, TronApiError, httpx.TimeoutException, httpx.TransportError, ValueError) as exc:
                self.failures += 1
                retryable = not isinstance(exc, TronApiError) or exc.retryable
                if not retryable or attempt > self.s.tron_max_retries:
                    if isinstance(exc, TronApiError):
                        raise
                    raise TronApiError(f"{type(exc).__name__}: {exc}", retryable=True) from exc
                if isinstance(exc, _RetryAfter):
                    delay = exc.seconds
                else:
                    delay = min(self.s.tron_retry_max_seconds, self.s.tron_retry_base_seconds * 2 ** (attempt - 1))
                    delay *= 0.5 + random.random() / 2  # jitter
                log.warning("TRON API request failed; retrying", path=path, attempt=attempt, delay=f"{delay:.1f}s", error=str(exc)[:120])
                await asyncio.sleep(delay)

    async def get_contract_events(
        self,
        *,
        min_timestamp_ms: int | None = None,
        max_timestamp_ms: int | None = None,
        fingerprint: str | None = None,
        only_confirmed: bool = False,
        only_unconfirmed: bool = False,
        limit: int = 200,
    ) -> tuple[list[dict[str, Any]], str | None]:
        params: dict[str, Any] = {
            "event_name": "Transfer",
            "order_by": "block_timestamp,asc",
            "limit": limit,
        }
        if min_timestamp_ms is not None:
            params["min_block_timestamp"] = min_timestamp_ms
        if max_timestamp_ms is not None:
            params["max_block_timestamp"] = max_timestamp_ms
        if fingerprint:
            params["fingerprint"] = fingerprint
        if only_confirmed:
            params["only_confirmed"] = "true"
        if only_unconfirmed:
            params["only_unconfirmed"] = "true"
        data = await self._request("GET", f"/v1/contracts/{self.s.usdt_contract_address}/events", params=params)
        if not isinstance(data, dict):
            raise TronApiError("unexpected response shape")
        events = data.get("data") or []
        meta = data.get("meta") or {}
        return list(events), meta.get("fingerprint") or None

    async def get_token_info(self) -> dict[str, Any]:
        """Read-only constant calls symbol()/decimals() to verify the configured contract."""
        out: dict[str, Any] = {}
        for fn in ("symbol()", "decimals()"):
            data = await self._request(
                "POST",
                "/wallet/triggerconstantcontract",
                json={
                    "owner_address": self.s.usdt_contract_address,
                    "contract_address": self.s.usdt_contract_address,
                    "function_selector": fn,
                    "visible": True,
                },
            )
            res = (data.get("constant_result") or [None])[0]
            if res:
                out[fn] = _decode_abi(fn, res)
        return out


class _RetryAfter(Exception):
    def __init__(self, seconds: float, status: int) -> None:
        super().__init__(f"HTTP {status} retry after {seconds}s")
        self.seconds = seconds


def _retry_after(resp: httpx.Response) -> float | None:
    v = resp.headers.get("Retry-After")
    if not v:
        return None
    try:
        return min(max(float(v), 0.5), 120.0)
    except ValueError:
        return None


def _decode_abi(fn: str, hexdata: str) -> Any:
    b = bytes.fromhex(hexdata)
    if fn == "decimals()":
        return int.from_bytes(b[:32], "big")
    try:
        length = int.from_bytes(b[32:64], "big")
        return b[64 : 64 + length].decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return b.rstrip(b"\0").decode("utf-8", "replace")


# --------------------------------------------------------------------------
# Collector
# --------------------------------------------------------------------------


@dataclass
class CollectorStats:
    confirmed_cursor_ms: int | None = None
    unconfirmed_cursor_ms: int | None = None
    last_confirmed_poll: float | None = None
    last_unconfirmed_poll: float | None = None
    backfill_next_ms: int | None = None
    backfill_end_ms: int | None = None
    backfill_done: bool = False
    api_errors: int = 0
    db_errors: int = 0
    rejected: dict[str, int] | None = None


ProcessFn = Callable[..., Awaitable[int]]


class TronCollector:
    def __init__(
        self,
        settings: Settings,
        source: EventSource,
        process: ProcessFn,
        session_factory,
        clock: Clock,
    ) -> None:
        self.s = settings
        self.source = source
        self.process = process
        self.sf = session_factory
        self.clock = clock
        self.stats = CollectorStats(rejected={})
        self._unconfirmed_cursor: int | None = None

    # ---------------------------------------------------------------- state
    async def _get_state(self, key: str) -> str | None:
        async with self.sf() as s:
            return await repo.get_state(s, key)

    async def _set_state(self, key: str, value: str) -> None:
        async with self.sf() as s, s.begin():
            await repo.set_state(s, key, value, self.clock.now())

    async def initialise(self) -> None:
        """Establish cursors on first start; keep them on every restart."""
        now_ms = datetime_to_ms(self.clock.now())
        cursor = await self._get_state(STATE_CONFIRMED_CURSOR)
        if cursor is None:
            # Live monitoring starts "now"; history before this point is the backfill's job.
            await self._set_state(STATE_CONFIRMED_CURSOR, str(now_ms - self.s.confirmed_overlap_seconds * 1000))
            if self.s.initial_history_days > 0:
                start = now_ms - self.s.initial_history_days * 86_400_000
                await self._set_state(STATE_BACKFILL_START, str(start))
                await self._set_state(STATE_BACKFILL_END, str(now_ms))
                await self._set_state(STATE_BACKFILL_NEXT, str(start))
            else:
                await self._set_state(STATE_BACKFILL_DONE, "1")
                await self._set_state(STATE_BACKFILL_ANALYSIS_DONE, "1")
            log.info("Collector initialised", live_from=ms_to_datetime(now_ms).isoformat(), history_days=self.s.initial_history_days)
        else:
            log.info("Collector resuming from saved cursor", cursor=ms_to_datetime(int(cursor)).isoformat())
        self.stats.confirmed_cursor_ms = int(await self._get_state(STATE_CONFIRMED_CURSOR))
        self.stats.backfill_done = (await self._get_state(STATE_BACKFILL_DONE)) == "1"

    def _count_rejections(self, rejected: dict[str, int]) -> None:
        for k, v in rejected.items():
            self.stats.rejected[k] = self.stats.rejected.get(k, 0) + v
        if rejected.get("malformed"):
            log.warning("Malformed events skipped", count=rejected["malformed"])

    # ---------------------------------------------------------------- confirmed
    async def poll_confirmed_once(self) -> bool:
        """Fetch and process confirmed events after the cursor. Returns True if
        more pages are immediately available (caller should poll again now)."""
        cursor = self.stats.confirmed_cursor_ms
        if cursor is None:
            cursor = int(await self._get_state(STATE_CONFIRMED_CURSOR))
        min_ts = cursor - self.s.confirmed_overlap_seconds * 1000
        fingerprint = None
        pages = 0
        while pages < self.s.max_pages_per_poll:
            raws, fingerprint = await self.source.get_contract_events(
                min_timestamp_ms=min_ts,
                fingerprint=fingerprint,
                only_confirmed=True,
                limit=self.s.tron_page_limit,
            )
            pages += 1
            if raws:
                events, rejected = parse_events(raws, self.s.usdt_contract_address, confirmed=True)
                self._count_rejections(rejected)
                await self.process(events, live=True)  # raises on DB failure -> cursor not advanced
                max_ts = max(int(r.get("block_timestamp") or 0) for r in raws if isinstance(r, dict))
                if max_ts > cursor:
                    cursor = max_ts
                    await self._set_state(STATE_CONFIRMED_CURSOR, str(cursor))
                    self.stats.confirmed_cursor_ms = cursor
            if not fingerprint or not raws:
                self.stats.last_confirmed_poll = time.time()
                return False
        self.stats.last_confirmed_poll = time.time()
        return True

    async def poll_unconfirmed_once(self) -> None:
        now_ms = datetime_to_ms(self.clock.now())
        floor = now_ms - self.s.unconfirmed_lookback_seconds * 1000
        min_ts = max(floor, (self._unconfirmed_cursor or floor) - self.s.confirmed_overlap_seconds * 1000)
        fingerprint = None
        for _ in range(max(1, self.s.max_pages_per_poll // 5)):
            raws, fingerprint = await self.source.get_contract_events(
                min_timestamp_ms=min_ts, fingerprint=fingerprint, only_unconfirmed=True, limit=self.s.tron_page_limit
            )
            if raws:
                events, rejected = parse_events(raws, self.s.usdt_contract_address, confirmed=False)
                self._count_rejections(rejected)
                await self.process(events, live=True)
                mx = max(int(r.get("block_timestamp") or 0) for r in raws if isinstance(r, dict))
                self._unconfirmed_cursor = max(self._unconfirmed_cursor or 0, mx)
                self.stats.unconfirmed_cursor_ms = self._unconfirmed_cursor
            if not fingerprint or not raws:
                break
        self.stats.last_unconfirmed_poll = time.time()

    async def _loop(self, name: str, fn: Callable[[], Awaitable[Any]], interval: float, stop: asyncio.Event) -> None:
        delay = 1.0
        while not stop.is_set():
            more = False
            try:
                more = bool(await fn())
                delay = 1.0
            except TronApiError as exc:
                self.stats.api_errors += 1
                log.error("TRON API unavailable; will retry", loop=name, error=str(exc)[:200], retry_in=f"{delay:.0f}s")
                await _sleep(stop, delay)
                delay = min(delay * 2, 60)
                continue
            except Exception as exc:  # noqa: BLE001
                if is_transient_db_error(exc):
                    self.stats.db_errors += 1
                    log.error("Database unavailable; will retry", loop=name, error=type(exc).__name__, retry_in=f"{delay:.0f}s")
                else:
                    log.exception("Collector loop error; will retry", loop=name)
                await _sleep(stop, delay)
                delay = min(delay * 2, 60)
                continue
            if not more:
                await _sleep(stop, interval)

    async def run_confirmed(self, stop: asyncio.Event) -> None:
        log.info("Monitor started", stream="confirmed", contract=self.s.usdt_contract_address)
        await self._loop("confirmed", self.poll_confirmed_once, self.s.poll_interval_seconds, stop)

    async def run_unconfirmed(self, stop: asyncio.Event) -> None:
        log.info("Monitor started", stream="unconfirmed")
        await self._loop("unconfirmed", self.poll_unconfirmed_once, self.s.unconfirmed_poll_interval_seconds, stop)

    # ---------------------------------------------------------------- backfill
    async def fetch_window(self, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        fingerprint = None
        for _ in range(100_000):
            raws, fingerprint = await self.source.get_contract_events(
                min_timestamp_ms=start_ms,
                max_timestamp_ms=end_ms - 1,
                fingerprint=fingerprint,
                only_confirmed=True,
                limit=self.s.tron_page_limit,
            )
            out.extend(raws)
            if not fingerprint or not raws:
                break
        return out

    async def run_backfill(self, stop: asyncio.Event, on_complete: Callable[[], Awaitable[None]] | None = None) -> None:
        """Historical backfill; resumable.  ``on_complete`` runs the pattern discovery."""
        if (await self._get_state(STATE_BACKFILL_DONE)) == "1":
            if (await self._get_state(STATE_BACKFILL_ANALYSIS_DONE)) != "1" and on_complete:
                await on_complete()
                await self._set_state(STATE_BACKFILL_ANALYSIS_DONE, "1")
            return
        nxt = int(await self._get_state(STATE_BACKFILL_NEXT) or 0)
        end = int(await self._get_state(STATE_BACKFILL_END) or 0)
        self.stats.backfill_end_ms = end
        step = self.s.backfill_window_seconds * 1000
        windows = [(t, min(t + step, end)) for t in range(nxt, end, step)]
        log.info(
            "Historical backfill started",
            start=ms_to_datetime(nxt).isoformat(),
            end=ms_to_datetime(end).isoformat(),
            windows=len(windows),
        )
        sem = asyncio.Semaphore(max(1, self.s.backfill_concurrency))

        async def fetch(w: tuple[int, int]) -> list[dict[str, Any]]:
            async with sem:
                delay = 1.0
                while True:
                    try:
                        return await self.fetch_window(*w)
                    except TronApiError as exc:
                        self.stats.api_errors += 1
                        log.error("Backfill window fetch failed; retrying", error=str(exc)[:200])
                        await _sleep(stop, delay)
                        if stop.is_set():
                            raise asyncio.CancelledError from exc
                        delay = min(delay * 2, 60)

        pending: list[asyncio.Task] = []
        idx = 0
        stored = 0
        started = time.time()
        try:
            while idx < len(windows) and not stop.is_set():
                while len(pending) < self.s.backfill_concurrency * 2 and idx + len(pending) < len(windows):
                    pending.append(asyncio.create_task(fetch(windows[idx + len(pending)])))
                raws = await pending.pop(0)
                events, rejected = parse_events(raws, self.s.usdt_contract_address, confirmed=True)
                self._count_rejections(rejected)
                while True:
                    try:
                        stored += await self.process(events, live=False)
                        break
                    except Exception as exc:  # noqa: BLE001
                        if not is_transient_db_error(exc):
                            raise
                        log.error("Database unavailable during backfill; retrying", error=type(exc).__name__)
                        await _sleep(stop, 5)
                        if stop.is_set():
                            return
                _, w_end = windows[idx]
                idx += 1
                await self._set_state(STATE_BACKFILL_NEXT, str(w_end))
                self.stats.backfill_next_ms = w_end
                if idx % 50 == 0 or idx == len(windows):
                    log.info(
                        "Backfill progress",
                        windows=f"{idx}/{len(windows)}",
                        reached=ms_to_datetime(w_end).isoformat(),
                        stored=stored,
                        elapsed=f"{time.time() - started:.0f}s",
                    )
        finally:
            for t in pending:
                t.cancel()
        if stop.is_set():
            return
        await self._set_state(STATE_BACKFILL_DONE, "1")
        self.stats.backfill_done = True
        log.info("Historical backfill complete", stored=stored)
        if on_complete:
            await on_complete()
        await self._set_state(STATE_BACKFILL_ANALYSIS_DONE, "1")


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
