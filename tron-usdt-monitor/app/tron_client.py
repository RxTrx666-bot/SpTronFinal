"""Async HTTP client for the TRON API (TronGrid or any java-tron compatible full node).

Endpoints used:
  GET  /v1/accounts/{addr}/transactions/trc20     TronGrid index (account mode, backfill)
  POST /wallet/gettransactioninfobyid             tx receipt + event logs (verification)
  POST /wallet/gettransactioninfobyblocknum       all receipts of a block (blocks mode)
  POST /wallet/getblock | /wallet/getnowblock     chain head
  POST /wallet/getblockbynum                      genesis check (mainnet)
  POST /wallet/triggerconstantcontract            symbol()/decimals() of the token contract
With CONFIRMED_ONLY=true the /walletsolidity/ equivalents (solidified blocks) are used.

All requests: timeout, retries with exponential backoff + jitter, 429/rate-limit handling
(Retry-After), latency measurement. The API key is sent as a header and never logged.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Awaitable, Callable

import httpx

from app.logger import kv

log = logging.getLogger(__name__)

MAINNET_GENESIS_BLOCK_ID = "00000000000000001ebf88508a03865c71d452e25f4d51194196a1d22b6653dc"


class TronApiError(Exception):
    def __init__(self, message: str, *, status: int | None = None, retryable: bool = True) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class ApiStats:
    def __init__(self) -> None:
        self.last_latency_ms: float | None = None
        self.avg_latency_ms: float | None = None
        self.requests = 0
        self.errors = 0
        self.rate_limited = 0
        self.last_success_ms: int | None = None
        self.last_error: str | None = None

    def record_latency(self, ms: float) -> None:
        self.last_latency_ms = ms
        self.avg_latency_ms = ms if self.avg_latency_ms is None else self.avg_latency_ms * 0.8 + ms * 0.2
        self.last_success_ms = int(time.time() * 1000)


def _is_rate_limit_text(text: str) -> bool:
    lowered = text.lower()
    return "rate" in lowered or "limit" in lowered or "frequency" in lowered or "too many" in lowered


class TronClient:
    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        api_key_header: str = "TRON-PRO-API-KEY",
        timeout: float = 10.0,
        max_retries: int = 4,
        backoff_base: float = 0.5,
        backoff_max: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        headers = {"Accept": "application/json", "User-Agent": "tron-usdt-monitor/1.0"}
        if api_key:
            headers[api_key_header] = api_key
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(timeout),
            transport=transport,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=10),
        )
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self._sleep = sleep
        self.stats = ApiStats()
        self._head_endpoint_supported = True

    async def close(self) -> None:
        await self._http.aclose()

    def _backoff(self, attempt: int) -> float:
        delay = min(self.backoff_max, self.backoff_base * (2**attempt))
        return delay + random.uniform(0, delay * 0.25)

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """Perform a request with retries. Raises TronApiError when all attempts fail."""
        last_error: TronApiError | None = None
        for attempt in range(self.max_retries + 1):
            started = time.perf_counter()
            self.stats.requests += 1
            retry_after: float | None = None
            try:
                response = await self._http.request(method, path, params=params, json=json)
                elapsed_ms = (time.perf_counter() - started) * 1000
                if response.status_code == 429 or (
                    response.status_code in (403, 503) and _is_rate_limit_text(response.text)
                ):
                    self.stats.rate_limited += 1
                    retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                    raise TronApiError("rate limited", status=response.status_code)
                if response.status_code >= 500:
                    raise TronApiError(f"server error HTTP {response.status_code}", status=response.status_code)
                if response.status_code >= 400:
                    raise TronApiError(
                        f"HTTP {response.status_code}: {response.text[:200]}",
                        status=response.status_code,
                        retryable=False,
                    )
                try:
                    data = response.json()
                except ValueError as exc:
                    raise TronApiError("invalid JSON in API response") from exc
                _raise_for_api_error(data)
                self.stats.record_latency(elapsed_ms)
                log.debug("tron_api_ok", extra=kv(path=path, latency_ms=round(elapsed_ms, 1)))
                return data
            except httpx.TimeoutException:
                last_error = TronApiError(f"timeout calling {path}")
            except httpx.TransportError as exc:
                last_error = TronApiError(f"network error calling {path}: {type(exc).__name__}")
            except TronApiError as exc:
                last_error = exc
                if not exc.retryable:
                    self.stats.errors += 1
                    self.stats.last_error = str(exc)
                    raise
            self.stats.errors += 1
            self.stats.last_error = str(last_error)
            if attempt >= self.max_retries:
                break
            delay = retry_after if retry_after is not None else self._backoff(attempt)
            log.warning(
                "tron_api_retry",
                extra=kv(
                    path=path,
                    attempt=attempt + 1,
                    max_attempts=self.max_retries + 1,
                    error=str(last_error),
                    status=last_error.status if last_error else None,
                    retry_in_s=round(delay, 2),
                ),
            )
            await self._sleep(delay)
        assert last_error is not None
        raise last_error

    # ------------------------------------------------------------------ endpoints

    async def get_trc20_transfers(
        self,
        address: str,
        contract: str,
        *,
        min_timestamp: int | None = None,
        max_timestamp: int | None = None,
        order: str = "asc",
        limit: int = 200,
        only_confirmed: bool = False,
        fingerprint: str | None = None,
        only_from: bool = False,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """TronGrid indexed TRC-20 transfers of an account (both directions)."""
        params: dict[str, Any] = {
            "contract_address": contract,
            "limit": limit,
            "order_by": f"block_timestamp,{order}",
        }
        if min_timestamp is not None:
            params["min_timestamp"] = int(min_timestamp)
        if max_timestamp is not None:
            params["max_timestamp"] = int(max_timestamp)
        if only_confirmed:
            params["only_confirmed"] = "true"
        if fingerprint:
            params["fingerprint"] = fingerprint
        if only_from:
            params["only_from"] = "true"
        data = await self.request("GET", f"/v1/accounts/{address}/transactions/trc20", params=params)
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise TronApiError("unexpected trc20 response shape")
        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        next_fp = meta.get("fingerprint") if isinstance(meta, dict) else None
        return data["data"], (next_fp if isinstance(next_fp, str) and next_fp else None)

    def _prefix(self, solidity: bool) -> str:
        return "/walletsolidity" if solidity else "/wallet"

    async def get_transaction_info(self, tx_hash: str, *, solidity: bool = False) -> dict[str, Any]:
        """Receipt + event logs. Returns {} when the tx is not (yet) in a block."""
        data = await self.request("POST", f"{self._prefix(solidity)}/gettransactioninfobyid", json={"value": tx_hash})
        if not isinstance(data, dict):
            raise TronApiError("unexpected gettransactioninfobyid response")
        return data

    async def get_transaction_info_by_block(self, number: int, *, solidity: bool = False) -> list[dict[str, Any]]:
        data = await self.request(
            "POST", f"{self._prefix(solidity)}/gettransactioninfobyblocknum", json={"num": int(number)}
        )
        if isinstance(data, dict) and not data:
            return []
        if not isinstance(data, list):
            raise TronApiError("unexpected gettransactioninfobyblocknum response")
        return data

    async def get_head_block(self, *, solidity: bool = False) -> tuple[int, int]:
        """(block number, block timestamp ms) of the current head (or solidified head)."""
        data: Any = None
        if self._head_endpoint_supported:
            try:
                # Header-only request (java-tron >= 4.7); much smaller than getnowblock.
                data = await self.request("POST", f"{self._prefix(solidity)}/getblock", json={"detail": False})
            except TronApiError as exc:
                if exc.retryable:
                    raise
                self._head_endpoint_supported = False
                log.info("getblock_unsupported_falling_back_to_getnowblock")
        if data is None or not isinstance(data, dict) or "block_header" not in data:
            data = await self.request("POST", f"{self._prefix(solidity)}/getnowblock", json={})
        try:
            raw = data["block_header"]["raw_data"]
            return int(raw["number"]), int(raw["timestamp"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TronApiError("unexpected block header response") from exc

    async def get_block_id(self, number: int) -> str:
        data = await self.request("POST", "/wallet/getblockbynum", json={"num": int(number)})
        if not isinstance(data, dict) or not isinstance(data.get("blockID"), str):
            raise TronApiError("unexpected getblockbynum response")
        return data["blockID"]

    async def call_constant(self, contract: str, selector: str, owner: str) -> str:
        data = await self.request(
            "POST",
            "/wallet/triggerconstantcontract",
            json={
                "owner_address": owner,
                "contract_address": contract,
                "function_selector": selector,
                "parameter": "",
                "visible": True,
            },
        )
        result = data.get("result") if isinstance(data, dict) else None
        constant = data.get("constant_result") if isinstance(data, dict) else None
        if not isinstance(result, dict) or result.get("result") is not True or not constant:
            message = result.get("message", "") if isinstance(result, dict) else ""
            try:
                message = bytes.fromhex(message).decode("utf-8", "replace")
            except ValueError:
                pass
            raise TronApiError(f"contract call {selector} failed: {message or 'no result'}", retryable=False)
        return str(constant[0])

    async def get_token_metadata(self, contract: str, owner: str) -> tuple[str, int]:
        symbol_hex = await self.call_constant(contract, "symbol()", owner)
        decimals_hex = await self.call_constant(contract, "decimals()", owner)
        return decode_abi_string(symbol_hex), int(decimals_hex or "0", 16)


def decode_abi_string(data: str) -> str:
    """Decode an ABI-encoded ``string`` return value (or a bytes32 fallback)."""
    data = data.removeprefix("0x")
    raw = bytes.fromhex(data)
    if len(raw) == 32:
        return raw.rstrip(b"\x00").decode("utf-8", "replace")
    offset = int.from_bytes(raw[0:32], "big")
    length = int.from_bytes(raw[offset : offset + 32], "big")
    return raw[offset + 32 : offset + 32 + length].decode("utf-8", "replace")


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, min(float(value), 120.0))
    except ValueError:
        return None


def _raise_for_api_error(data: Any) -> None:
    """TronGrid/java-tron sometimes return errors with HTTP 200."""
    if not isinstance(data, dict):
        return
    if data.get("success") is False:
        message = str(data.get("error") or "API reported success=false")
        raise TronApiError(message, retryable=_is_rate_limit_text(message) or "timeout" in message.lower())
    error = data.get("Error") or data.get("error")
    if isinstance(error, str) and error and "data" not in data:
        raise TronApiError(error, retryable=_is_rate_limit_text(error))
