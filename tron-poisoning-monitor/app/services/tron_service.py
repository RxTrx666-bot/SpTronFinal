"""READ-ONLY TRON data access (TronGrid-compatible HTTP API).

Endpoints used (all reads):

* ``POST /wallet/getnowblock``                     latest block (head)
* ``POST /wallet/getblockbynum``                   block + transactions (signer / owner_address)
* ``POST /wallet/gettransactioninfobyblocknum``    receipts + event logs for a whole block
* ``POST /walletsolidity/getnowblock``             latest solidified (irreversible) block
* ``POST /wallet/gettransactionbyid``              transaction details (signer)
* ``POST /walletsolidity/gettransactioninfobyid``  confirmed receipt (block, status)
* ``POST /wallet/gettransactioninfobyid``          unconfirmed receipt
* ``GET  /v1/accounts/{address}``                  account info (creation time)
* ``GET  /v1/accounts/{address}/transactions/trc20`` TRC-20 transfer history
* ``GET  {TRONSCAN}/api/accountv2``                optional public address tag

This module never builds, signs or broadcasts transactions and never handles
keys; there is no code path that calls a write endpoint
(``tests/test_readonly.py`` enforces this).

TronGrid does not offer WebSocket/event subscriptions; the low-latency path
is block-by-block polling (one block every ~3 s), which costs a constant
2 requests per block regardless of how many wallets are watched.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Protocol

import httpx

from app.domain import AccountInfo, AddressLabel, BlockData, Contact, TokenTransfer, TxDetails, assign_sequences
from app.utils.address import InvalidAddress, base58_to_hex, hex_to_base58, normalize_address, try_normalize
from app.utils.logging import get_logger
from app.utils.ratelimit import PRIORITY_LIVE, PriorityRateLimiter

log = get_logger(__name__)

TRANSFER_TOPIC = "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


class TronApiError(Exception):
    def __init__(self, message: str, *, retryable: bool = True, status: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class TronDataSource(Protocol):
    """Everything the application needs from the chain.  Implemented by
    :class:`TronGridClient` (mainnet) and the in-memory simulator."""

    async def get_now_block_number(self) -> int: ...
    async def get_solid_block_number(self) -> int: ...
    async def get_block(self, number: int, contracts: dict[str, int]) -> BlockData: ...
    async def get_trc20_transfers(
        self,
        address: str,
        contract: str,
        *,
        min_timestamp_ms: int | None = None,
        max_timestamp_ms: int | None = None,
        only_confirmed: bool = False,
        order: str = "asc",
        limit: int = 200,
        fingerprint: str | None = None,
        priority: int = PRIORITY_LIVE,
    ) -> tuple[list[TokenTransfer], str | None]: ...
    async def get_transaction(self, tx_hash: str, priority: int = PRIORITY_LIVE) -> TxDetails: ...
    async def get_account(self, address: str, priority: int = PRIORITY_LIVE) -> AccountInfo: ...
    async def get_label(self, address: str) -> AddressLabel | None: ...
    async def close(self) -> None: ...


class _RetryAfter(Exception):
    def __init__(self, seconds: float, status: int) -> None:
        super().__init__(f"HTTP {status}, retry after {seconds}s")
        self.seconds = seconds


def parse_block_transfers(
    number: int,
    block: dict[str, Any],
    infos: list[dict[str, Any]],
    contracts: dict[str, int],
    trx_dust_max_sun: int = 1_000_000,
) -> BlockData:
    """Decode TRC-20 Transfer logs of the configured token contracts from one block.

    ``contracts`` maps Base58 contract -> decimals (only used to filter).
    Failed / reverted transactions are skipped (they emit no state change).

    Also returns ``contacts``: Transfer events of OTHER TRC-20 contracts (fake "USDT"
    tokens are the classic poisoning vehicle) and TRX transfers of at most
    ``trx_dust_max_sun`` - both ways an attacker plants a look-alike in a wallet's history.
    """
    contacts: list[Contact] = []
    header = (block.get("block_header") or {}).get("raw_data") or {}
    ts = int(header.get("timestamp") or 0)
    contract_hex = {}
    for c in contracts:
        try:
            contract_hex[base58_to_hex(c)[2:]] = c
        except InvalidAddress:
            continue
    owners: dict[str, str] = {}
    for tx in block.get("transactions") or []:
        try:
            contract = tx["raw_data"]["contract"][0]
            value = contract["parameter"]["value"]
            owner = try_normalize(value.get("owner_address"))
            txid = str(tx.get("txID", "")).lower()
            if owner:
                owners[txid] = owner
            if contract.get("type") == "TransferContract" and owner:
                ok = ((tx.get("ret") or [{}])[0].get("contractRet") or "SUCCESS") == "SUCCESS"
                to = try_normalize(value.get("to_address"))
                amount = int(value.get("amount") or 0)
                if ok and to and to != owner and 0 <= amount <= trx_dust_max_sun:
                    contacts.append(Contact(owner, to, "TRX", txid, ts, amount))
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    transfers: list[TokenTransfer] = []
    for info in infos or []:
        if info.get("result") == "FAILED":
            continue
        receipt = info.get("receipt") or {}
        if receipt.get("result") not in (None, "SUCCESS"):
            continue
        txid = str(info.get("id", "")).lower()
        ts_tx = int(info.get("blockTimeStamp") or ts)
        for idx, lg in enumerate(info.get("log") or []):
            addr = str(lg.get("address", "")).lower()
            if addr.startswith("41") and len(addr) == 42:
                addr = addr[2:]
            token = contract_hex.get(addr)
            topics = lg.get("topics") or []
            if len(topics) < 3 or str(topics[0]).lower().removeprefix("0x") != TRANSFER_TOPIC:
                continue
            if token is None:  # another TRC-20 token: record as a contact in both directions
                try:
                    a, b = hex_to_base58(str(topics[1])), hex_to_base58(str(topics[2]))
                    other = hex_to_base58(addr)
                    amt = int(str(lg.get("data") or "0").removeprefix("0x")[:64] or "0", 16)
                except (InvalidAddress, ValueError):
                    continue
                if a != b:
                    contacts.append(Contact(a, b, "TOKEN", txid, ts_tx, amt, other))
                    contacts.append(Contact(b, a, "TOKEN", txid, ts_tx, amt, other))
                continue
            try:
                frm = hex_to_base58(str(topics[1]))
                to = hex_to_base58(str(topics[2]))
                data = str(lg.get("data") or "0").removeprefix("0x")
                amount = int(data[:64] or "0", 16)
            except (InvalidAddress, ValueError):
                log.warning("MALFORMED_LOG", tx=txid, block=number)
                continue
            transfers.append(
                TokenTransfer(
                    tx_hash=txid,
                    token_contract=token,
                    from_address=frm,
                    to_address=to,
                    amount=amount,
                    block_timestamp_ms=ts_tx,
                    block_number=number,
                    confirmed=False,
                    initiator=owners.get(txid),
                    log_index=idx,
                )
            )
    return BlockData(number=number, timestamp_ms=ts, transfers=assign_sequences(transfers), contacts=contacts)


def parse_trc20_history(items: list[dict[str, Any]], contract: str, *, confirmed: bool) -> list[TokenTransfer]:
    out: list[TokenTransfer] = []
    for it in items:
        if it.get("type") not in (None, "Transfer"):
            continue
        token = try_normalize((it.get("token_info") or {}).get("address"))
        if token != contract:
            continue
        frm, to = try_normalize(it.get("from")), try_normalize(it.get("to"))
        try:
            amount = int(str(it.get("value", "")))
            ts = int(it.get("block_timestamp"))
        except (TypeError, ValueError):
            continue
        txid = str(it.get("transaction_id", "")).lower()
        if not frm or not to or len(txid) != 64 or amount < 0:
            continue
        out.append(
            TokenTransfer(
                tx_hash=txid,
                token_contract=token,
                from_address=frm,
                to_address=to,
                amount=amount,
                block_timestamp_ms=ts,
                confirmed=confirmed,
            )
        )
    # assign seq per tx in API order (identical tuples inside one tx are rare)
    return assign_sequences(out)


def label_category(label: str) -> str:
    low = label.lower()
    exchanges = (
        "binance", "okx", "okex", "huobi", "htx", "kucoin", "bybit", "gate", "bitget", "mexc", "poloniex",
        "kraken", "bitfinex", "coinbase", "crypto.com", "bithumb", "upbit", "exchange", "hot wallet", "deposit",
    )  # fmt: skip
    if any(x in low for x in ("scam", "phish", "fraud", "hack", "exploit")):
        return "flagged"
    if any(x in low for x in exchanges):
        return "exchange"
    return "service"


class TronGridClient:
    """Async TronGrid client: pooled connections, global rate limit, retries with
    exponential backoff + jitter, Retry-After support and per-request timeouts."""

    def __init__(self, settings, transport: httpx.AsyncBaseTransport | None = None, limiter: PriorityRateLimiter | None = None) -> None:
        self.s = settings
        headers = {"Accept": "application/json", "User-Agent": "tron-poisoning-monitor/1.0 (read-only)"}
        if settings.tron_api_key:
            headers[settings.tron_api_key_header] = settings.tron_api_key
        limits = httpx.Limits(max_connections=settings.tron_max_connections, max_keepalive_connections=settings.tron_max_connections)
        self._client = httpx.AsyncClient(
            base_url=settings.tron_api_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(settings.tron_request_timeout_seconds),
            transport=transport,
            limits=limits,
        )
        scan_headers = {"Accept": "application/json"}
        if settings.tronscan_api_key:
            scan_headers["TRON-PRO-API-KEY"] = settings.tronscan_api_key
        self._scan = httpx.AsyncClient(base_url=settings.tronscan_api_url.rstrip("/"), headers=scan_headers, timeout=httpx.Timeout(10.0), transport=transport)
        self.limiter = limiter or PriorityRateLimiter(settings.tron_rate_limit_rps)
        self.requests = 0
        self.failures = 0
        self.last_error: str | None = None
        self.last_ok_at: float | None = None
        self._head_cache: tuple[int, dict] | None = None

    async def close(self) -> None:
        await self._client.aclose()
        await self._scan.aclose()

    async def _request(self, method: str, path: str, *, priority: int = PRIORITY_LIVE, max_retries: int | None = None, **kwargs) -> Any:
        retries = self.s.tron_max_retries if max_retries is None else max_retries
        attempt = 0
        while True:
            attempt += 1
            await self.limiter.acquire(priority)
            try:
                self.requests += 1
                resp = await self._client.request(method, path, **kwargs)
                if resp.status_code == 429 or resp.status_code >= 500:
                    ra = resp.headers.get("Retry-After")
                    if ra:
                        try:
                            raise _RetryAfter(min(max(float(ra), 0.5), 120.0), resp.status_code)
                        except ValueError:
                            pass
                    raise TronApiError(f"HTTP {resp.status_code}", retryable=True, status=resp.status_code)
                if resp.status_code >= 400:
                    raise TronApiError(f"HTTP {resp.status_code}: {resp.text[:200]}", retryable=False, status=resp.status_code)
                data = resp.json()
                if isinstance(data, dict) and data.get("success") is False:
                    raise TronApiError(f"API error: {str(data.get('error'))[:200]}", retryable=True)
                if isinstance(data, dict) and "Error" in data and len(data) == 1:
                    raise TronApiError(f"node error: {str(data['Error'])[:200]}", retryable=False)
                self.last_ok_at = time.time()
                return data
            except (_RetryAfter, TronApiError, httpx.TimeoutException, httpx.TransportError, ValueError) as exc:
                self.failures += 1
                self.last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
                retryable = not isinstance(exc, TronApiError) or exc.retryable
                if not retryable or attempt > retries:
                    if isinstance(exc, TronApiError):
                        raise
                    raise TronApiError(f"{type(exc).__name__}: {exc}", retryable=True) from exc
                if isinstance(exc, _RetryAfter):
                    delay = exc.seconds
                else:
                    delay = min(self.s.tron_retry_max_seconds, self.s.tron_retry_base_seconds * 2 ** (attempt - 1))
                    delay *= 0.5 + random.random() / 2
                log.warning("TRON_API_RETRY", path=path, attempt=attempt, delay=f"{delay:.2f}s", api_status=getattr(exc, "status", None), error=str(exc)[:120])
                await asyncio.sleep(delay)

    # ------------------------------------------------------------------ blocks
    async def get_now_block_number(self) -> int:
        data = await self._request("POST", "/wallet/getnowblock", json={})
        number = int(data["block_header"]["raw_data"]["number"])
        self._head_cache = (number, data)
        return number

    async def get_solid_block_number(self) -> int:
        data = await self._request("POST", "/walletsolidity/getnowblock", json={})
        return int(data["block_header"]["raw_data"]["number"])

    async def get_block(self, number: int, contracts: dict[str, int]) -> BlockData:
        trx_dust = self.s.units("network_trx_dust_max", 6)
        if self._head_cache and self._head_cache[0] == number:
            block_coro = asyncio.sleep(0, result=self._head_cache[1])
        else:
            block_coro = self._request("POST", "/wallet/getblockbynum", json={"num": number})
        info_coro = self._request("POST", "/wallet/gettransactioninfobyblocknum", json={"num": number})
        block, infos = await asyncio.gather(block_coro, info_coro)
        if not block or "block_header" not in block:
            raise TronApiError(f"block {number} not available yet", retryable=True)
        if isinstance(infos, dict):  # empty block returns {}
            infos = []
        return parse_block_transfers(number, block, infos, contracts, trx_dust)

    # ------------------------------------------------------------------ account history
    async def get_trc20_transfers(
        self,
        address: str,
        contract: str,
        *,
        min_timestamp_ms: int | None = None,
        max_timestamp_ms: int | None = None,
        only_confirmed: bool = False,
        order: str = "asc",
        limit: int = 200,
        fingerprint: str | None = None,
        priority: int = PRIORITY_LIVE,
    ) -> tuple[list[TokenTransfer], str | None]:
        params: dict[str, Any] = {
            "contract_address": contract,
            "limit": min(limit, 200),
            "order_by": f"block_timestamp,{order}",
        }
        if min_timestamp_ms is not None:
            params["min_timestamp"] = min_timestamp_ms
        if max_timestamp_ms is not None:
            params["max_timestamp"] = max_timestamp_ms
        if only_confirmed:
            params["only_confirmed"] = "true"
        if fingerprint:
            params["fingerprint"] = fingerprint
        data = await self._request("GET", f"/v1/accounts/{normalize_address(address)}/transactions/trc20", params=params, priority=priority)
        items = data.get("data") or [] if isinstance(data, dict) else []
        meta = data.get("meta") or {} if isinstance(data, dict) else {}
        return parse_trc20_history(items, contract, confirmed=only_confirmed), (meta.get("fingerprint") or None)

    # ------------------------------------------------------------------ transactions
    async def get_transaction(self, tx_hash: str, priority: int = PRIORITY_LIVE) -> TxDetails:
        h = tx_hash.lower()
        tx, solid = await asyncio.gather(
            self._request("POST", "/wallet/gettransactionbyid", json={"value": h}, priority=priority),
            self._request("POST", "/walletsolidity/gettransactioninfobyid", json={"value": h}, priority=priority),
        )
        initiator = None
        success = None
        if tx:
            try:
                initiator = try_normalize(tx["raw_data"]["contract"][0]["parameter"]["value"]["owner_address"])
            except (KeyError, IndexError, TypeError):
                initiator = None
            ret = (tx.get("ret") or [{}])[0].get("contractRet")
            if ret:
                success = ret == "SUCCESS"
        if solid:
            receipt = solid.get("receipt") or {}
            if receipt.get("result"):
                success = receipt.get("result") == "SUCCESS"
            if solid.get("result") == "FAILED":
                success = False
            return TxDetails(
                tx_hash=h,
                found=True,
                block_number=solid.get("blockNumber"),
                block_timestamp_ms=solid.get("blockTimeStamp"),
                success=success,
                initiator=initiator,
                confirmed=True,
            )
        info = await self._request("POST", "/wallet/gettransactioninfobyid", json={"value": h}, priority=priority)
        return TxDetails(
            tx_hash=h,
            found=bool(tx) or bool(info),
            block_number=(info or {}).get("blockNumber"),
            block_timestamp_ms=(info or {}).get("blockTimeStamp"),
            success=success,
            initiator=initiator,
            confirmed=False,
        )

    async def get_account(self, address: str, priority: int = PRIORITY_LIVE) -> AccountInfo:
        data = await self._request("GET", f"/v1/accounts/{normalize_address(address)}", priority=priority)
        rows = data.get("data") or [] if isinstance(data, dict) else []
        if not rows:
            return AccountInfo(address=address, exists=False)
        row = rows[0]
        return AccountInfo(address=address, exists=True, create_time_ms=row.get("create_time"), trx_balance_sun=row.get("balance"))

    async def get_label(self, address: str) -> AddressLabel | None:
        """Public address tag from TronScan (best effort; failures return None)."""
        if not self.s.labels_enabled:
            return None
        try:
            await self.limiter.acquire(2)
            resp = await self._scan.get("/api/accountv2", params={"address": normalize_address(address)})
            if resp.status_code != 200:
                return None
            data = resp.json()
        except (httpx.HTTPError, ValueError, InvalidAddress):
            return None
        tag = None
        for key in ("addressTag", "publicTag", "tag", "name"):
            v = data.get(key) if isinstance(data, dict) else None
            if isinstance(v, str) and v.strip():
                tag = v.strip()
                break
        if not tag:
            return None
        return AddressLabel(address=address, label=tag[:200], category=label_category(tag), source="tronscan public tag")
