"""Read-only TronGrid HTTP client.

Only the endpoints in ``_ALLOWED_PATHS`` can be called; anything that could
create, sign or broadcast a transaction is rejected before a request is sent.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import httpx

log = logging.getLogger(__name__)

_ALLOWED_PATHS = (
    "/v1/accounts/",  # .../transactions/trc20
    "/wallet/gettransactioninfobyid",
    "/walletsolidity/gettransactioninfobyid",
    "/wallet/triggerconstantcontract",
)
_FORBIDDEN_FRAGMENTS = ("broadcast", "createtransaction", "sign", "transfer", "freeze", "deploy")


class TronError(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


def _check_path(path: str) -> None:
    lowered = path.lower()
    if not any(lowered.startswith(p) for p in _ALLOWED_PATHS):
        raise PermissionError(f"read-only client refuses path {path!r}")
    if lowered.startswith("/wallet") and any(f in lowered for f in _FORBIDDEN_FRAGMENTS):
        raise PermissionError(f"read-only client refuses path {path!r}")


class TronClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 10.0,
        max_attempts: int = 4,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers = {"Accept": "application/json", "User-Agent": "tron-outflow-alert/1.0"}
        if api_key:
            headers["TRON-PRO-API-KEY"] = api_key
        self._client = httpx.AsyncClient(
            base_url=base_url, headers=headers, timeout=httpx.Timeout(timeout), transport=transport
        )
        self._max_attempts = max(1, max_attempts)

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None) -> Any:
        _check_path(path)
        delay = 0.5
        last: TronError | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                resp = await self._client.request(method, path, params=params, json=json)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = TronError(f"network error: {type(exc).__name__}", retryable=True)
            else:
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except ValueError:
                        last = TronError("invalid JSON from TronGrid", retryable=True)
                elif resp.status_code == 429 or resp.status_code >= 500:
                    last = TronError(f"TronGrid HTTP {resp.status_code}", retryable=True)
                    retry_after = resp.headers.get("Retry-After", "")
                    if retry_after.isdigit():
                        delay = max(delay, min(float(retry_after), 30.0))
                else:
                    raise TronError(f"TronGrid HTTP {resp.status_code}: {resp.text[:200]}", retryable=False)
            if attempt < self._max_attempts:
                await asyncio.sleep(delay + random.uniform(0, delay / 2))
                delay = min(delay * 2, 8.0)
        assert last is not None
        raise last

    # ------------------------------------------------------------------ listing
    async def list_outgoing_trc20(
        self,
        wallet: str,
        contract: str,
        *,
        min_timestamp_ms: int,
        limit: int = 200,
        fingerprint: str | None = None,
    ) -> tuple[list[dict], str | None]:
        """One page of outgoing TRC-20 transfers, oldest first.  Returns (items, next_fingerprint)."""
        params: dict[str, Any] = {
            "contract_address": contract,
            "only_from": "true",
            "limit": limit,
            "min_timestamp": max(0, int(min_timestamp_ms)),
            "order_by": "block_timestamp,asc",
        }
        if fingerprint:
            params["fingerprint"] = fingerprint
        data = await self._request("GET", f"/v1/accounts/{wallet}/transactions/trc20", params=params)
        if not isinstance(data, dict) or data.get("success") is False:
            raise TronError(f"TronGrid listing error: {str(data)[:200]}", retryable=True)
        items = data.get("data") or []
        if not isinstance(items, list):
            raise TronError("TronGrid listing: 'data' is not a list", retryable=True)
        meta = data.get("meta") or {}
        # TronGrid only includes a fingerprint when another page exists.
        next_fp = meta.get("fingerprint") if items else None
        return items, (str(next_fp) if next_fp else None)

    # -------------------------------------------------------------- tx receipts
    async def get_transaction_info(self, txid: str, *, solidified: bool = False) -> dict:
        """Transaction receipt + event logs.  Returns ``{}`` while the tx is not yet in a block."""
        path = "/walletsolidity/gettransactioninfobyid" if solidified else "/wallet/gettransactioninfobyid"
        data = await self._request("POST", path, json={"value": txid})
        if not isinstance(data, dict):
            raise TronError("gettransactioninfobyid: unexpected response", retryable=True)
        return data

    # ----------------------------------------------------------- constant calls
    async def call_constant(self, contract: str, selector: str, owner: str) -> str:
        """Run a view function via triggerconstantcontract and return the first result word(s) as hex."""
        payload = {
            "owner_address": owner,
            "contract_address": contract,
            "function_selector": selector,
            "parameter": "",
            "visible": True,
        }
        data = await self._request("POST", "/wallet/triggerconstantcontract", json=payload)
        result = (data or {}).get("result") or {}
        if not result.get("result"):
            raise TronError(f"triggerconstantcontract {selector} failed: {str(data)[:200]}", retryable=False)
        constant = data.get("constant_result") or []
        if not constant or not isinstance(constant[0], str):
            raise TronError(f"triggerconstantcontract {selector}: empty result", retryable=False)
        return constant[0]
