"""In-memory fakes of TronGrid and the Telegram Bot API, served through httpx.MockTransport."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx

from outflow_alert.abi import TRANSFER_TOPIC
from outflow_alert.address import address_from_seed, base58_to_hex
from outflow_alert.config import Settings

WALLET = "TWkvffFDMsqbmTLkMHMABmw452Hyq98cdn"
USDT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
OTHER = address_from_seed("recipient")
FAKE_TOKEN = address_from_seed("fake-usdt")


def make_settings(**overrides) -> Settings:
    base = dict(
        tron_api_key="test-key",
        telegram_bot_token="123:abc",
        telegram_chat_ids=("-1001",),
        db_path=":memory:",
        poll_interval_seconds=0.5,
        lookback_seconds=120,
        page_limit=2,
    )
    base.update(overrides)
    return Settings(**base)


def abi_string(text: str) -> str:
    data = text.encode()
    padded = data.hex().ljust(((len(data) + 31) // 32) * 64, "0")
    return f"{32:064x}{len(data):064x}{padded}"


def topic(address: str) -> str:
    return "0" * 24 + base58_to_hex(address)[2:]


def transfer_log(contract: str, sender: str, recipient: str, amount: int) -> dict:
    return {
        "address": base58_to_hex(contract)[2:],
        "topics": [TRANSFER_TOPIC, topic(sender), topic(recipient)],
        "data": f"{amount:064x}",
    }


def txid(n: int) -> str:
    return f"{n:064x}"


@dataclass
class FakeTronGrid:
    listing: list[dict] = field(default_factory=list)  # TronGrid trc20 items
    infos: dict[str, dict] = field(default_factory=dict)  # txid -> gettransactioninfobyid
    symbol: str = "USDT"
    decimals: int = 6
    fail_listing: int = 0  # number of upcoming listing calls that return 503
    calls: list[tuple[str, str]] = field(default_factory=list)
    headers_seen: list[dict] = field(default_factory=list)

    def add_transfer(self, n: int, amount: int, *, ts: int, sender: str = WALLET, recipient: str = OTHER,
                     contract: str = USDT, receipt: str = "SUCCESS", log_contract: str | None = None,
                     listed: bool = True, info: bool = True) -> str:
        tid = txid(n)
        if listed:
            self.listing.append({
                "transaction_id": tid,
                "token_info": {"symbol": "USDT", "address": contract, "decimals": 6, "name": "Tether USD"},
                "block_timestamp": ts,
                "from": sender,
                "to": recipient,
                "type": "Transfer",
                "value": str(amount),
            })
        if info:
            self.infos[tid] = {
                "id": tid,
                "blockNumber": 70_000_000 + n,
                "blockTimeStamp": ts,
                "contract_address": base58_to_hex(contract),
                "receipt": {"result": receipt, "energy_usage_total": 64285},
                "log": [transfer_log(log_contract or contract, sender, recipient, amount)],
            }
        return tid

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        self.headers_seen.append(dict(request.headers))
        if path.endswith("/transactions/trc20"):
            if self.fail_listing:
                self.fail_listing -= 1
                return httpx.Response(503, json={"error": "unavailable"})
            q = request.url.params
            assert q["only_from"] == "true" and q["contract_address"] == USDT
            min_ts = int(q["min_timestamp"])
            limit = int(q["limit"])
            offset = int(q.get("fingerprint", "0"))
            rows = sorted((r for r in self.listing if r["block_timestamp"] >= min_ts), key=lambda r: r["block_timestamp"])
            page = rows[offset : offset + limit]
            meta: dict = {"at": 0, "page_size": len(page)}
            if offset + limit < len(rows):
                meta["fingerprint"] = str(offset + limit)
                meta["links"] = {"next": "https://api.trongrid.io/next"}
            return httpx.Response(200, json={"data": page, "success": True, "meta": meta})
        body = json.loads(request.content or b"{}")
        if path.endswith("/gettransactioninfobyid"):
            return httpx.Response(200, json=self.infos.get(body["value"], {}))
        if path == "/wallet/triggerconstantcontract":
            assert body["contract_address"] == USDT
            if body["function_selector"] == "symbol()":
                result = abi_string(self.symbol)
            else:
                result = f"{self.decimals:064x}"
            return httpx.Response(200, json={"result": {"result": True}, "constant_result": [result]})
        return httpx.Response(404, json={"Error": "unknown"})


@dataclass
class FakeTelegram:
    sent: list[dict] = field(default_factory=list)          # sendMessage payloads
    calls: list[tuple[str, dict]] = field(default_factory=list)  # every other method
    updates: list[dict] = field(default_factory=list)       # served once by getUpdates
    fail_next: list[httpx.Response] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": "outflow_bot"}})
        if method == "getUpdates":
            pending = [u for u in self.updates if u["update_id"] >= body.get("offset", 0)]
            self.updates = []
            return httpx.Response(200, json={"ok": True, "result": pending})
        if method != "sendMessage":
            self.calls.append((method, body))
            return httpx.Response(200, json={"ok": True, "result": True})
        if self.fail_next:
            return self.fail_next.pop(0)
        self.sent.append(body)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.sent)}})
