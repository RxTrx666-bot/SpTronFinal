"""Shared fixtures: realistic TronGrid / java-tron payload builders and fakes."""

from __future__ import annotations

import asyncio
from typing import Any

from app.config import Settings
from app.database import SQLiteRepository
from app.filters import TransferFilter
from app.stats import MonitorStats
from app.transaction_parser import TRANSFER_EVENT_TOPIC
from app.tron_address import base58_to_hex, hex_to_base58
from app.tron_monitor import TransactionProcessor

WALLET = "TWkvffFDMsqbmTLkMHMABmw452Hyq98cdn"
USDT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
OTHER = hex_to_base58("41" + "11" * 20)
THIRD = hex_to_base58("41" + "22" * 20)
FAKE_USDT = hex_to_base58("41" + "33" * 20)  # a scam token also named "USDT"
BLOCK_TS = 1_790_000_000_000  # 2026-09-21 ...


def tx_hash(n: int) -> str:
    return f"{n:064x}"


def make_settings(**overrides: str) -> Settings:
    env = {
        "TELEGRAM_BOT_TOKEN": "123456:TEST-TOKEN-abcdefghijklmnop",
        "TELEGRAM_ADMIN_CHAT_ID": "1001",
        "TRON_API_KEY": "49a5e403-0000-0000-0000-test00000000",
        "WALLET_ADDRESS": WALLET,
        "USDT_CONTRACT": USDT,
        "DATABASE_URL": "sqlite://:memory:",
        "HEARTBEAT_FILE": "/tmp/claude-0/tron-test-heartbeat",
        "ALERT_DIRECTIONS": "INCOMING,OUTGOING",
    }
    env.update(overrides)
    return Settings.from_env(env)


def trongrid_record(
    n: int,
    sender: str = OTHER,
    recipient: str = WALLET,
    value: str = "1100000",
    ts: int = BLOCK_TS,
    contract: str = USDT,
    symbol: str = "USDT",
    decimals: int = 6,
    type_: str = "Transfer",
) -> dict[str, Any]:
    """Shape of an item of GET /v1/accounts/{addr}/transactions/trc20 (TronGrid)."""
    return {
        "transaction_id": tx_hash(n),
        "token_info": {"symbol": symbol, "address": contract, "decimals": decimals, "name": "Tether USD"},
        "block_timestamp": ts,
        "from": sender,
        "to": recipient,
        "type": type_,
        "value": value,
    }


def topic(address: str) -> str:
    return "0" * 24 + base58_to_hex(address)[2:]


def transfer_log(sender: str, recipient: str, amount: int, contract: str = USDT) -> dict[str, Any]:
    """Shape of a TransactionInfo.log[] entry (java-tron: address without 41 prefix)."""
    return {
        "address": base58_to_hex(contract)[2:],
        "topics": [TRANSFER_EVENT_TOPIC, topic(sender), topic(recipient)],
        "data": f"{amount:064x}",
    }


def tx_info(n: int, logs: list[dict[str, Any]], block: int = 70_000_000, ts: int = BLOCK_TS,
            result: str = "SUCCESS") -> dict[str, Any]:
    """Shape of POST /wallet/gettransactioninfobyid response."""
    info: dict[str, Any] = {
        "id": tx_hash(n),
        "fee": 345000,
        "blockNumber": block,
        "blockTimeStamp": ts,
        "contractResult": [""],
        "contract_address": base58_to_hex(USDT),
        "receipt": {"energy_usage_total": 14650, "net_usage": 345, "result": result},
        "log": logs,
    }
    if result != "SUCCESS":
        info["result"] = "FAILED"
    return info


def trx_transfer_info(n: int) -> dict[str, Any]:
    """A plain TRX TransferContract has a receipt but no contract logs."""
    return {"id": tx_hash(n), "blockNumber": 70_000_000, "blockTimeStamp": BLOCK_TS,
            "receipt": {"net_usage": 268}}


class FakeAlerts:
    def __init__(self) -> None:
        self.queued: list[str] = []

    async def enqueue(self, tx_hash: str) -> None:
        self.queued.append(tx_hash)


async def make_processor(settings: Settings | None = None, repo: SQLiteRepository | None = None,
                         clock=None):
    settings = settings or make_settings()
    if repo is None:
        repo = SQLiteRepository(":memory:")
        await repo.init()
    alerts = FakeAlerts()
    stats = MonitorStats()
    flt = TransferFilter(settings.wallet_address, settings.usdt_contract, settings.min_raw, settings.max_raw,
                         directions=settings.alert_directions)
    kwargs = {"clock": clock} if clock else {}
    return TransactionProcessor(settings, flt, repo, alerts, stats, **kwargs), repo, alerts, stats


def run(coro):
    return asyncio.run(coro)
