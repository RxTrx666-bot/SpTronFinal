"""In-memory event source that mimics TronGrid's contract-events API
(time filtering, ascending order, fingerprint pagination, confirmed /
unconfirmed separation).  Used by the simulator and the automated tests so the
real collector, parser and pipeline are exercised end-to-end."""

from __future__ import annotations

import hashlib
import itertools
from decimal import Decimal
from typing import Any

from app.amounts import usdt_to_raw
from app.collector.address import base58_to_hex

_counter = itertools.count(1)


def tx_hash(seed: str | None = None) -> str:
    seed = seed or f"tx-{next(_counter)}"
    return hashlib.sha256(seed.encode()).hexdigest()


def make_event(
    *,
    sender: str,
    recipient: str,
    amount_usdt: Decimal | str | int,
    ts_ms: int,
    contract: str,
    txid: str | None = None,
    event_index: int = 0,
    block: int | None = None,
    unconfirmed: bool = False,
) -> dict[str, Any]:
    """Build a raw event exactly as TronGrid returns it (addresses as 0x-hex)."""
    raw = {
        "block_number": block if block is not None else ts_ms // 3000,
        "block_timestamp": ts_ms,
        "caller_contract_address": contract,
        "contract_address": contract,
        "event_index": event_index,
        "event_name": "Transfer",
        "result": {
            "from": "0x" + base58_to_hex(sender)[2:],
            "to": "0x" + base58_to_hex(recipient)[2:],
            "value": str(usdt_to_raw(amount_usdt)),
        },
        "result_type": {"from": "address", "to": "address", "value": "uint256"},
        "event": "Transfer(address indexed from, address indexed to, uint256 value)",
        "transaction_id": txid or tx_hash(),
    }
    if unconfirmed:
        raw["_unconfirmed"] = True
    return raw


class SimulatedEventSource:
    def __init__(self) -> None:
        self.confirmed: list[dict[str, Any]] = []
        self.unconfirmed: list[dict[str, Any]] = []
        self.fail_next = 0
        self.calls = 0

    def add(self, raw: dict[str, Any]) -> dict[str, Any]:
        (self.unconfirmed if raw.get("_unconfirmed") else self.confirmed).append(raw)
        return raw

    def confirm(self, txid: str) -> None:
        """Move an unconfirmed event to the confirmed stream (block solidified)."""
        for raw in list(self.unconfirmed):
            if raw["transaction_id"] == txid:
                self.unconfirmed.remove(raw)
                c = dict(raw)
                c.pop("_unconfirmed", None)
                self.confirmed.append(c)

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
        from app.collector.tron_listener import TronApiError

        self.calls += 1
        if self.fail_next > 0:
            self.fail_next -= 1
            raise TronApiError("simulated TRON API outage")
        if only_confirmed:
            pool = self.confirmed
        elif only_unconfirmed:
            pool = self.unconfirmed
        else:
            pool = self.confirmed + self.unconfirmed
        items = [
            r
            for r in pool
            if (min_timestamp_ms is None or r.get("block_timestamp", 0) >= min_timestamp_ms)
            and (max_timestamp_ms is None or r.get("block_timestamp", 0) <= max_timestamp_ms)
        ]
        items.sort(key=lambda r: (r.get("block_timestamp", 0), r.get("transaction_id", ""), r.get("event_index", 0)))
        offset = int(fingerprint or 0)
        page = items[offset : offset + limit]
        nxt = str(offset + limit) if offset + limit < len(items) else None
        return page, nxt

    async def get_token_info(self) -> dict[str, Any]:
        return {"symbol()": "USDT", "decimals()": 6}

    async def close(self) -> None:
        return None
