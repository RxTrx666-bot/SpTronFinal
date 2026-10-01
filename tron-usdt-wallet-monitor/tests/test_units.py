"""Pure unit tests: amounts, addresses, normalizer, config, API client, logging, schema, read-only."""

from __future__ import annotations

import asyncio
import io
import logging
import re
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.amounts import base_to_usdt, fmt_usdt, usdt_to_base
from app.logging_setup import configure_logging, get_logger, register_secret
from app.tron.address import is_valid_tron_address, normalize_address
from app.tron.client import RateLimiter, TronApiError, TronGridClient
from app.tron.normalizer import TRANSFER_TOPIC, parse_event, parse_events, parse_tx_info_logs
from tests.conftest import make_settings
from tests.fakes import OTHER_TOKEN, T0, USDT, WALLET_A, addr, make_event

W1 = addr("wallet-1")


# ------------------------------------------------------------ amounts


def test_amounts_are_exact_integers():
    assert usdt_to_base("500") == 500_000_000
    assert usdt_to_base("0.000001") == 1
    assert usdt_to_base("499.999999") == 499_999_999
    assert base_to_usdt(500_000_001) == Decimal("500.000001")
    assert fmt_usdt(2_500_000_000) == "2,500"
    assert fmt_usdt(1) == "0.000001"
    assert fmt_usdt(10_000_000_000_000_000) == "10,000,000,000"
    with pytest.raises(ValueError):
        usdt_to_base("0.0000001")
    with pytest.raises(TypeError):
        usdt_to_base(0.1)  # floats are refused


def test_settings_threshold_and_validation():
    s = make_settings(alert_min_amount_usdt="500")
    assert s.alert_min_base_units == 500_000_000
    assert make_settings(database_url="postgresql://u:p@h/db").database_url == "postgresql+asyncpg://u:p@h/db"
    with pytest.raises(ValueError):
        make_settings(root_wallet="TNotARealAddress123")
    with pytest.raises(ValueError):
        make_settings(alert_min_amount_usdt="500.0000001")
    with pytest.raises(ValueError):
        make_settings(telegram_admin_chat_id="@me")
    assert "123456:TEST-TOKEN-abcdef" not in repr(s)


# ------------------------------------------------------------ addresses


def test_address_validation():
    assert is_valid_tron_address(WALLET_A)
    assert is_valid_tron_address("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t")
    assert not is_valid_tron_address("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6u")  # bad checksum
    assert not is_valid_tron_address("0x" + "00" * 20)
    assert not is_valid_tron_address("")
    assert normalize_address("0x" + "a6" * 20).startswith("T")


# ------------------------------------------------------------ normalizer


def test_parse_valid_event():
    t = parse_event(make_event(WALLET_A, W1, 10_000, ts=T0, idx=3, unconfirmed=True), USDT)
    assert (t.from_address, t.to_address, t.amount_base_units, t.event_index, t.confirmed) == (WALLET_A, W1, 10_000, 3, False)
    assert parse_event(make_event(WALLET_A, W1, 1, ts=T0), USDT).confirmed is True


def test_parse_rejects_and_malformed():
    good = make_event(WALLET_A, W1, 1, ts=T0)
    bad_addr = {**good, "result": {**good["result"], "to": "0xzz"}}
    no_tx = {**good, "transaction_id": None}
    huge = {**good, "result": {**good["result"], "value": str(2**200)}}
    approval = {**good, "event_name": "Approval"}
    zero = {**good, "result": {**good["result"], "value": "0"}}
    other = make_event(WALLET_A, W1, 1, ts=T0, contract=OTHER_TOKEN)
    out, rej = parse_events([good, bad_addr, no_tx, huge, approval, zero, other, "junk", None], USDT)
    assert len(out) == 1
    assert rej == {"malformed": 5, "not_transfer_event": 1, "zero_amount": 1, "not_usdt": 1}


def test_parse_tx_info_logs():
    from app.tron.address import base58_to_hex

    usdt_hex = base58_to_hex(USDT)[2:]
    pad = lambda a: "0" * 24 + base58_to_hex(a)[2:]  # noqa: E731
    info = {
        "id": "ab" * 32,
        "blockNumber": 123,
        "blockTimeStamp": T0,
        "log": [
            {"address": usdt_hex, "topics": ["8c5be1e5" + "0" * 56, pad(WALLET_A), pad(W1)], "data": "%064x" % 5},  # Approval
            {"address": usdt_hex, "topics": [TRANSFER_TOPIC, pad(WALLET_A), pad(W1)], "data": "%064x" % 777},
        ],
    }
    [t] = parse_tx_info_logs(info, USDT)
    assert (t.event_index, t.from_address, t.to_address, t.amount_base_units, t.block_number) == (1, WALLET_A, W1, 777, 123)


# ------------------------------------------------------------ API client


def _client(handler, **overrides) -> TronGridClient:
    s = make_settings(tron_retry_base_seconds=0.001, tron_retry_max_seconds=0.002, **overrides)
    return TronGridClient(s, transport=httpx.MockTransport(handler))


async def test_client_retries_rate_limit_and_5xx():
    calls = []

    def handler(req: httpx.Request):
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        if len(calls) == 2:
            return httpx.Response(503)
        return httpx.Response(200, json={"data": [{"x": 1}], "meta": {"fingerprint": "fp"}})

    c = _client(handler)
    rows, fp = await c.get_contract_events(min_timestamp_ms=1, fingerprint=None, only_confirmed=False, limit=200)
    assert rows == [{"x": 1}] and fp == "fp" and len(calls) == 3
    assert calls[0].headers["TRON-PRO-API-KEY"] == "test-api-key-000000"
    assert calls[0].url.params["min_block_timestamp"] == "1"
    await c.close()


async def test_client_gives_up_and_non_retryable():
    def handler(req):
        return httpx.Response(400, text="bad request")

    c = _client(handler)
    with pytest.raises(TronApiError) as e:
        await c.get_transaction_events("ab" * 32)
    assert e.value.retryable is False
    await c.close()

    n = []

    def timeout(req):
        n.append(1)
        raise httpx.ConnectTimeout("timeout")

    c = _client(timeout, tron_max_retries=2)
    with pytest.raises(TronApiError):
        await c.get_transaction_events("ab" * 32)
    assert len(n) == 3
    await c.close()


async def test_client_concurrency_is_capped():
    active = 0
    peak = 0

    async def handler(req):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, json={"data": []})

    c = _client(handler, max_concurrent_api_requests=3, max_requests_per_second=1000)
    await asyncio.gather(*(c.get_transaction_events("ab" * 32) for _ in range(30)))
    assert peak == 3
    await c.close()


async def test_rate_limiter():
    import time

    rl = RateLimiter(20)
    start = time.monotonic()
    for _ in range(40):
        await rl.acquire()
    assert time.monotonic() - start >= 0.9  # 20 burst + 20 more at 20/s


# ------------------------------------------------------------ logging


def test_secrets_are_redacted_from_logs():
    configure_logging("INFO", "text")
    buf = io.StringIO()
    root = logging.getLogger()
    root.handlers[0].stream = buf
    register_secret("1111111111:SECRET-BOT-TOKEN")
    get_logger("t").error("failed calling https://api.telegram.org/bot1111111111:SECRET-BOT-TOKEN/sendMessage", key="1111111111:SECRET-BOT-TOKEN")
    out = buf.getvalue()
    assert "SECRET-BOT-TOKEN" not in out and "***" in out


# ------------------------------------------------------------ schema


async def test_migration_matches_models(engine):
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    from app.db.models import Base

    def diff(conn):
        return compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)

    async with engine.connect() as conn:
        changes = await conn.run_sync(diff)
    assert changes == []


# ------------------------------------------------------------ read-only


def test_application_is_read_only():
    src = "\n".join(p.read_text() for p in Path("app").rglob("*.py"))
    flat = src.lower().replace("_", "")
    for forbidden in ("broadcasttransaction", "createtransaction", "signtransaction", "triggersmartcontract", "privatekey", "mnemonic"):
        assert forbidden not in flat, forbidden
    endpoints = set(re.findall(r'"(/wallet[a-z]*/[a-z]+)"', src))
    assert endpoints <= {"/wallet/gettransactioninfobyid", "/wallet/triggerconstantcontract"}
