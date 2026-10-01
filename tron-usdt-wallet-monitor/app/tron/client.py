"""READ-ONLY async TronGrid client.

Only HTTP *read* endpoints are used.  This module never builds, signs or
broadcasts transactions and never handles private keys.

Every request from every component (live stream, scheduler, backfill) goes
through one global ``asyncio.Semaphore`` (MAX_CONCURRENT_API_REQUESTS) and one
token-bucket rate limiter (MAX_REQUESTS_PER_SECOND), so monitoring thousands of
wallets can never cause an uncontrolled burst of API calls.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Protocol

import httpx

from app.config import Settings
from app.logging_setup import get_logger

log = get_logger(__name__)


class TronApiError(Exception):
    def __init__(self, message: str, *, retryable: bool = True, status: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class TronSource(Protocol):
    """What the monitors need from the API (the fake in tests implements this too)."""

    async def get_contract_events(
        self, *, min_timestamp_ms: int | None, fingerprint: str | None, only_confirmed: bool, limit: int
    ) -> tuple[list[dict[str, Any]], str | None]: ...

    async def get_account_trc20(
        self,
        address: str,
        *,
        direction: str,
        min_timestamp_ms: int | None,
        max_timestamp_ms: int | None,
        fingerprint: str | None,
        limit: int,
    ) -> tuple[list[dict[str, Any]], str | None]: ...

    async def get_transaction_events(self, tx_hash: str) -> list[dict[str, Any]]: ...

    async def get_transaction_info(self, tx_hash: str) -> dict[str, Any]: ...


class RateLimiter:
    """Token bucket: at most ``rate`` acquisitions per second (burst = max(1, rate))."""

    def __init__(self, rate: float) -> None:
        self.rate = rate
        self.capacity = max(1.0, rate)
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                await asyncio.sleep((1 - self._tokens) / self.rate)


class _RetryAfter(Exception):
    def __init__(self, seconds: float, status: int) -> None:
        super().__init__(f"HTTP {status} (retry after {seconds:.1f}s)")
        self.seconds = seconds


class TronGridClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.s = settings
        headers = {"Accept": "application/json", "User-Agent": "tron-usdt-wallet-monitor/1.0"}
        key = settings.tron_api_key.get_secret_value()
        if key:
            headers["TRON-PRO-API-KEY"] = key
        self._client = httpx.AsyncClient(
            base_url=settings.tron_api_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(settings.tron_request_timeout_seconds),
            transport=transport,
        )
        self._sem = asyncio.Semaphore(settings.max_concurrent_api_requests)
        self._limiter = RateLimiter(settings.max_requests_per_second)
        # stats (read by /status and /health)
        self.requests = 0
        self.failures = 0
        self.last_success_at: float | None = None
        self.last_latency_ms: float | None = None

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        attempt = 0
        while True:
            attempt += 1
            try:
                async with self._sem:
                    await self._limiter.acquire()
                    self.requests += 1
                    started = time.perf_counter()
                    resp = await self._client.request(method, path, **kwargs)
                    latency = (time.perf_counter() - started) * 1000
                if resp.status_code == 429 or resp.status_code >= 500:
                    ra = _retry_after(resp)
                    if ra is not None:
                        raise _RetryAfter(ra, resp.status_code)
                    raise TronApiError(f"HTTP {resp.status_code}", retryable=True, status=resp.status_code)
                if resp.status_code >= 400:
                    raise TronApiError(f"HTTP {resp.status_code}: {resp.text[:200]}", retryable=False, status=resp.status_code)
                data = resp.json()
                if isinstance(data, dict) and data.get("success") is False:
                    raise TronApiError(f"API error: {str(data.get('error'))[:200]}", retryable=True)
                self.last_success_at = time.time()
                self.last_latency_ms = latency
                log.debug("TRON API ok", path=path.split("?")[0], latency_ms=f"{latency:.0f}")
                return data
            except (_RetryAfter, TronApiError, httpx.TimeoutException, httpx.TransportError, ValueError) as exc:
                self.failures += 1
                retryable = not isinstance(exc, TronApiError) or exc.retryable
                if not retryable or attempt > self.s.tron_max_retries:
                    log.error("TRON API request failed", path=_safe_path(path), attempts=attempt, error=str(exc)[:200])
                    if isinstance(exc, TronApiError):
                        raise
                    raise TronApiError(f"{type(exc).__name__}: {exc}", retryable=True) from exc
                if isinstance(exc, _RetryAfter):
                    delay = exc.seconds
                else:
                    delay = min(self.s.tron_retry_max_seconds, self.s.tron_retry_base_seconds * 2 ** (attempt - 1))
                    delay *= 0.5 + random.random() / 2  # jitter
                log.warning("TRON API retry", path=_safe_path(path), attempt=attempt, delay=f"{delay:.1f}s", error=str(exc)[:120])
                await asyncio.sleep(delay)

    # ------------------------------------------------------------- endpoints
    async def get_contract_events(
        self, *, min_timestamp_ms: int | None, fingerprint: str | None, only_confirmed: bool, limit: int
    ) -> tuple[list[dict[str, Any]], str | None]:
        """USDT ``Transfer`` events of the whole contract, oldest first."""
        params: dict[str, Any] = {"event_name": "Transfer", "order_by": "block_timestamp,asc", "limit": limit}
        if min_timestamp_ms is not None:
            params["min_block_timestamp"] = min_timestamp_ms
        if fingerprint:
            params["fingerprint"] = fingerprint
        if only_confirmed:
            params["only_confirmed"] = "true"
        data = await self._request("GET", f"/v1/contracts/{self.s.usdt_contract}/events", params=params)
        return _page(data)

    async def get_account_trc20(
        self,
        address: str,
        *,
        direction: str,
        min_timestamp_ms: int | None,
        max_timestamp_ms: int | None,
        fingerprint: str | None,
        limit: int,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """USDT TRC-20 transfers of one account (``direction`` = "in" | "out"), oldest first."""
        params: dict[str, Any] = {
            "contract_address": self.s.usdt_contract,
            "order_by": "block_timestamp,asc",
            "limit": limit,
            "only_to" if direction == "in" else "only_from": "true",
        }
        if self.s.require_confirmed:
            params["only_confirmed"] = "true"
        if min_timestamp_ms is not None:
            params["min_timestamp"] = min_timestamp_ms
        if max_timestamp_ms is not None:
            params["max_timestamp"] = max_timestamp_ms
        if fingerprint:
            params["fingerprint"] = fingerprint
        data = await self._request("GET", f"/v1/accounts/{address}/transactions/trc20", params=params)
        return _page(data)

    async def get_transaction_events(self, tx_hash: str) -> list[dict[str, Any]]:
        data = await self._request("GET", f"/v1/transactions/{tx_hash}/events")
        return _page(data)[0]

    async def get_transaction_info(self, tx_hash: str) -> dict[str, Any]:
        data = await self._request("POST", "/wallet/gettransactioninfobyid", json={"value": tx_hash})
        return data if isinstance(data, dict) else {}

    async def get_token_info(self) -> dict[str, Any]:
        """Read-only constant calls ``symbol()`` / ``decimals()`` on the configured contract."""
        out: dict[str, Any] = {}
        for fn in ("symbol()", "decimals()"):
            data = await self._request(
                "POST",
                "/wallet/triggerconstantcontract",
                json={
                    "owner_address": self.s.usdt_contract,
                    "contract_address": self.s.usdt_contract,
                    "function_selector": fn,
                    "visible": True,
                },
            )
            res = ((data or {}).get("constant_result") or [None])[0]
            if res:
                out[fn.rstrip("()")] = _decode_abi(fn, res)
        return out


def _page(data: Any) -> tuple[list[dict[str, Any]], str | None]:
    if not isinstance(data, dict):
        raise TronApiError("unexpected response shape")
    items = data.get("data") or []
    meta = data.get("meta") or {}
    return [i for i in items if isinstance(i, dict)], meta.get("fingerprint") or None


def _safe_path(path: str) -> str:
    return path.split("?")[0]


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
