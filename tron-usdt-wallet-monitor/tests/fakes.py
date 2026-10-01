"""In-memory fakes for the TRON API and Telegram (no network in tests)."""

from __future__ import annotations

import hashlib
import itertools
from collections import Counter
from typing import Any

from app.telegram.client import TelegramError
from app.tron.address import address_from_seed, base58_to_hex
from app.tron.client import TronApiError

USDT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
OTHER_TOKEN = "TEkxiTehnzSmSe2XqrBj4w32RUN966rdz8"  # USDC on TRON
WALLET_A = address_from_seed("wallet-a")
T0 = 1_790_000_000_000  # a fixed "now" in ms (2026-09-21)

_counter = itertools.count(1)


def addr(name: str) -> str:
    return address_from_seed(name)


def usdt(amount: str) -> int:
    from app.amounts import usdt_to_base

    return usdt_to_base(amount)


def _hex0x(address: str) -> str:
    return "0x" + base58_to_hex(address)[2:]


def make_event(
    frm: str,
    to: str,
    amount: int,
    *,
    ts: int,
    tx: str | None = None,
    idx: int = 0,
    block: int | None = None,
    contract: str = USDT,
    unconfirmed: bool = False,
) -> dict[str, Any]:
    n = next(_counter)
    ev = {
        "transaction_id": tx or hashlib.sha256(f"tx{n}".encode()).hexdigest(),
        "block_number": block if block is not None else 70_000_000 + (ts - T0) // 3000,
        "block_timestamp": ts,
        "contract_address": contract,
        "event_index": idx,
        "event_name": "Transfer",
        "result": {"from": _hex0x(frm), "to": _hex0x(to), "value": str(amount)},
        "result_type": {"from": "address", "to": "address", "value": "uint256"},
    }
    if unconfirmed:
        ev["_unconfirmed"] = True
    return ev


class FakeTron:
    """Implements app.tron.client.TronSource over a list of raw events."""

    def __init__(self, contract: str = USDT) -> None:
        self.contract = contract
        self.events: list[dict[str, Any]] = []
        self.hidden_from_stream: set[str] = set()
        self.calls: Counter[str] = Counter()
        self.fail_stream = 0  # raise TronApiError for the next N stream calls
        self.min_ts_seen: list[int | None] = []

    def add(self, frm: str, to: str, amount: int, *, ts: int, **kw) -> dict[str, Any]:
        ev = make_event(frm, to, amount, ts=ts, **kw)
        self.events.append(ev)
        return ev

    def _ordered(self) -> list[dict[str, Any]]:
        return sorted(self.events, key=lambda e: (e["block_timestamp"], e["transaction_id"], e["event_index"]))

    @staticmethod
    def _paginate(rows: list, fingerprint: str | None, limit: int) -> tuple[list, str | None]:
        start = int(fingerprint or 0)
        page = rows[start : start + limit]
        nxt = start + limit
        return page, (str(nxt) if nxt < len(rows) else None)

    async def get_contract_events(self, *, min_timestamp_ms, fingerprint, only_confirmed, limit):
        self.calls["contract_events"] += 1
        self.min_ts_seen.append(min_timestamp_ms)
        if self.fail_stream:
            self.fail_stream -= 1
            raise TronApiError("HTTP 503")
        rows = [
            e
            for e in self._ordered()
            if e["contract_address"] == self.contract
            and e["transaction_id"] not in self.hidden_from_stream
            and (min_timestamp_ms is None or e["block_timestamp"] >= min_timestamp_ms)
            and not (only_confirmed and e.get("_unconfirmed"))
        ]
        return self._paginate(rows, fingerprint, limit)

    async def get_account_trc20(self, address, *, direction, min_timestamp_ms, max_timestamp_ms, fingerprint, limit):
        self.calls["account_trc20"] += 1
        from app.tron.address import hex_to_base58

        rows = []
        for e in self._ordered():
            if e["contract_address"] != self.contract:
                continue
            frm, to = hex_to_base58(e["result"]["from"]), hex_to_base58(e["result"]["to"])
            if (direction == "in" and to != address) or (direction == "out" and frm != address):
                continue
            ts = e["block_timestamp"]
            if (min_timestamp_ms is not None and ts < min_timestamp_ms) or (max_timestamp_ms is not None and ts > max_timestamp_ms):
                continue
            rows.append(
                {
                    "transaction_id": e["transaction_id"],
                    "token_info": {"symbol": "USDT", "address": self.contract, "decimals": 6, "name": "Tether USD"},
                    "block_timestamp": ts,
                    "from": frm,
                    "to": to,
                    "type": "Transfer",
                    "value": e["result"]["value"],
                }
            )
        return self._paginate(rows, fingerprint, limit)

    async def get_transaction_events(self, tx_hash):
        self.calls["tx_events"] += 1
        return [e for e in self.events if e["transaction_id"] == tx_hash]

    async def get_transaction_info(self, tx_hash):
        self.calls["tx_info"] += 1
        return {}


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict | None]] = []
        self.edited: list[tuple[int, str]] = []
        self.fail_times = 0
        self.crash_after_send = False

    async def send_message(self, chat_id, text, reply_markup=None):
        if self.fail_times:
            self.fail_times -= 1
            raise TelegramError("telegram 502: Bad Gateway")
        self.sent.append((str(chat_id), text, reply_markup))
        if self.crash_after_send:
            raise SystemExit("simulated crash after Telegram accepted the message")
        return str(len(self.sent))

    async def edit_message(self, chat_id, message_id, text, reply_markup=None):
        self.edited.append((message_id, text))

    async def answer_callback(self, callback_id):
        pass
