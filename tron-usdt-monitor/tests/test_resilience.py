"""API failure / retry / recovery behaviour, using the real HTTP clients with mocked transports."""

import asyncio
import json
import logging

import httpx
import pytest

from app.logger import RedactingFormatter, register_secrets
from app.stats import MonitorStats
from app.telegram_bot import AlertDispatcher, TelegramApiError, TelegramClient
from app.tron_client import TronApiError, TronClient, decode_abi_string
from app.tron_monitor import AccountMonitor
from tests.helpers import BLOCK_TS, make_processor, make_settings, run, trongrid_record, tx_hash
from tests.test_monitor import FakeTron


class Recorder:
    def __init__(self):
        self.delays = []

    async def __call__(self, seconds):
        self.delays.append(seconds)


def tron_client(handler, retries=3):
    sleep = Recorder()
    client = TronClient("https://api.trongrid.io", api_key="KEY-123456", max_retries=retries,
                        transport=httpx.MockTransport(handler), sleep=sleep)
    return client, sleep


def test_retries_then_succeeds_on_server_errors_and_timeouts():
    attempts = []

    def handler(request):
        attempts.append(request)
        if len(attempts) == 1:
            raise httpx.ConnectTimeout("timeout")
        if len(attempts) == 2:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json={"data": [trongrid_record(1)], "success": True, "meta": {}})

    async def go():
        client, sleep = tron_client(handler)
        records, fp = await client.get_trc20_transfers("W", "C", min_timestamp=1)
        assert len(records) == 1 and fp is None
        assert len(sleep.delays) == 2 and sleep.delays[1] > sleep.delays[0] * 1.5  # exponential backoff
        assert attempts[-1].headers["TRON-PRO-API-KEY"] == "KEY-123456"
        assert attempts[-1].url.params["contract_address"] == "C"
        assert attempts[-1].url.params["order_by"] == "block_timestamp,asc"
        await client.close()
    run(go())


def test_rate_limit_respects_retry_after():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, text="Too Many Requests")
        return httpx.Response(200, json={"blockID": "00", "block_header": {"raw_data": {"number": 5, "timestamp": 9}}})

    async def go():
        client, sleep = tron_client(handler)
        assert await client.get_head_block() == (5, 9)
        assert sleep.delays == [3.0] and client.stats.rate_limited == 1
        await client.close()
    run(go())


def test_gives_up_after_max_retries():
    async def go():
        client, sleep = tron_client(lambda r: httpx.Response(503, text="unavailable"), retries=2)
        with pytest.raises(TronApiError):
            await client.get_transaction_info(tx_hash(1))
        assert len(sleep.delays) == 2
        await client.close()
    run(go())


def test_non_retryable_client_error_fails_fast():
    async def go():
        client, sleep = tron_client(lambda r: httpx.Response(400, text="bad request"))
        with pytest.raises(TronApiError) as info:
            await client.get_transaction_info(tx_hash(1))
        assert info.value.retryable is False and sleep.delays == []
        await client.close()
    run(go())


def test_malformed_json_is_retried():
    responses = [httpx.Response(200, text="<html>oops</html>"), httpx.Response(200, json={})]

    async def go():
        client, sleep = tron_client(lambda r: responses.pop(0))
        assert await client.get_transaction_info(tx_hash(1)) == {}
        await client.close()
    run(go())


def test_head_block_falls_back_to_getnowblock():
    def handler(request):
        if request.url.path.endswith("/getblock"):
            return httpx.Response(405, text="method not allowed")
        return httpx.Response(200, json={"block_header": {"raw_data": {"number": 77, "timestamp": 1}}})

    async def go():
        client, _ = tron_client(handler)
        assert await client.get_head_block() == (77, 1)
        await client.close()
    run(go())


def test_contract_metadata_decoding():
    symbol_word = (
        "0000000000000000000000000000000000000000000000000000000000000020"
        "0000000000000000000000000000000000000000000000000000000000000004"
        + "USDT".encode().hex().ljust(64, "0")
    )

    def handler(request):
        body = json.loads(request.content)
        value = symbol_word if body["function_selector"] == "symbol()" else f"{6:064x}"
        return httpx.Response(200, json={"result": {"result": True}, "constant_result": [value]})

    async def go():
        client, _ = tron_client(handler)
        assert await client.get_token_metadata("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", "W") == ("USDT", 6)
        await client.close()
    run(go())
    assert decode_abi_string("55534454" + "0" * 56) == "USDT"


def test_monitor_loop_survives_api_outage():
    async def go():
        settings = make_settings(VERIFY_EVENT_LOG="false", POLL_INTERVAL_SECONDS="0.2")
        processor, repo, alerts, stats = await make_processor(settings)
        tron = FakeTron()
        stop = asyncio.Event()
        sleeps = []

        async def fake_sleep(s):
            sleeps.append(s)
            if len(sleeps) == 3:
                tron.records.append(trongrid_record(1, ts=BLOCK_TS + 1000))
            if len(sleeps) >= 6:
                stop.set()

        mon = AccountMonitor(settings, tron, repo, processor, stats, sleep=fake_sleep)
        await mon.initialize()
        stats.initialized = True
        tron.fail_next = 3  # the next 3 polls fail (API down)
        await asyncio.wait_for(mon.run(stop), timeout=5)
        assert stats.poll_errors == 3 and stats.initialized
        assert sleeps[:3] == [1.0, 2.0, 4.0]  # exponential backoff on outage
        assert alerts.queued == [tx_hash(1)]
    run(go())


def telegram(handler):
    return TelegramClient("123:SECRET", transport=httpx.MockTransport(handler))


def test_alert_dispatcher_retries_until_delivered_and_marks_sent():
    sent = []
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            raise httpx.ConnectError("down")
        if state["n"] == 2:
            return httpx.Response(429, json={"ok": False, "error_code": 429, "description": "Too Many Requests",
                                             "parameters": {"retry_after": 2}})
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    async def go():
        settings = make_settings()
        processor, repo, _, stats = await make_processor(settings)
        from app.transaction_parser import parse_trongrid_trc20_record
        await processor.process(parse_trongrid_trc20_record(trongrid_record(1)))
        sleeper = Recorder()
        dispatcher = AlertDispatcher(settings, telegram(handler), repo, stats, sleep=sleeper)
        assert await dispatcher.load_pending() == 1
        await dispatcher.drain()
        assert len(sent) == 2 and all(m["chat_id"] == 1001 for m in sent)
        assert "WALLET CREATED" in sent[0]["text"]  # wallet-created first, then the alert
        assert "tronscan.org/#/transaction/" + tx_hash(1) in sent[1]["text"]
        assert (await repo.get_transaction(tx_hash(1))).alert_status == "sent"
        assert 2.0 <= sleeper.delays[1] <= 2.25  # honoured retry_after
        # a restarted dispatcher finds nothing pending -> no duplicate alert
        dispatcher2 = AlertDispatcher(settings, telegram(handler), repo, stats, sleep=sleeper)
        assert await dispatcher2.load_pending() == 0
    run(go())


def test_telegram_error_parsing():
    async def go():
        client = telegram(lambda r: httpx.Response(400, json={"ok": False, "error_code": 400,
                                                              "description": "Bad Request: chat not found"}))
        with pytest.raises(TelegramApiError) as info:
            await client.send_message(1, "x")
        assert not info.value.retryable
        await client.close()
    run(go())


def test_secrets_redacted_from_logs():
    register_secrets(["123456:TEST-TOKEN-abcdefghijklmnop", "49a5e403-0000-0000-0000-test00000000"])
    record = logging.LogRecord("x", logging.ERROR, __file__, 1,
                               "failed https://api.telegram.org/bot123456:TEST-TOKEN-abcdefghijklmnop/send",
                               None, None)
    record.ctx = {"header": "49a5e403-0000-0000-0000-test00000000"}
    for json_mode in (False, True):
        line = RedactingFormatter(json_mode=json_mode).format(record)
        assert "TEST-TOKEN" not in line and "49a5e403" not in line and "***" in line
