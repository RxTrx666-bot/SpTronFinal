"""Failure-recovery tests: TRON API (29/30), Telegram, collector cursor safety."""

from datetime import timedelta

import httpx
import pytest

from app.collector.tron_listener import TronApiError, TronGridClient
from app.config.settings import OFFICIAL_USDT_TRC20_CONTRACT as USDT
from app.domain import datetime_to_ms
from app.simulation.source import make_event
from app.telegram.alerts import RecordingSink
from app.telegram.bot import TelegramClient, TelegramSink
from tests.conftest import make_settings
from tests.helpers import addr, kinds


def _event(ts=1_758_600_000_000):
    return make_event(sender=addr("A"), recipient=addr("B"), amount_usdt=5, ts_ms=ts, contract=USDT)


# 30. TRON API failure recovery (timeouts, 5xx, 429, then success)
async def test_trongrid_client_retries_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert request.url.path == f"/v1/contracts/{USDT}/events"
        assert request.headers["TRON-PRO-API-KEY"] == "k"
        assert request.url.params["only_confirmed"] == "true"
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("timeout")
        if calls["n"] == 2:
            return httpx.Response(503, text="unavailable")
        if calls["n"] == 3:
            return httpx.Response(429, headers={"Retry-After": "0.01"}, json={})
        return httpx.Response(200, json={"data": [_event()], "meta": {"fingerprint": "fp2"}})

    s = make_settings("sqlite+aiosqlite://", tron_api_key="k")
    client = TronGridClient(s, transport=httpx.MockTransport(handler))
    events, fp = await client.get_contract_events(min_timestamp_ms=1, only_confirmed=True)
    await client.close()
    assert len(events) == 1 and fp == "fp2" and calls["n"] == 4


async def test_trongrid_client_gives_up_and_raises():
    s = make_settings("sqlite+aiosqlite://", tron_max_retries=2)
    client = TronGridClient(s, transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(TronApiError):
        await client.get_contract_events(min_timestamp_ms=1)
    await client.close()


async def test_trongrid_client_pagination_params():
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        if "fingerprint" not in request.url.params:
            return httpx.Response(200, json={"data": [_event()], "meta": {"fingerprint": "abc"}})
        return httpx.Response(200, json={"data": [_event(1_758_600_003_000)], "meta": {}})

    s = make_settings("sqlite+aiosqlite://")
    client = TronGridClient(s, transport=httpx.MockTransport(handler))
    _, fp = await client.get_contract_events(min_timestamp_ms=10, max_timestamp_ms=20)
    _, fp2 = await client.get_contract_events(min_timestamp_ms=10, fingerprint=fp)
    await client.close()
    assert seen[0]["order_by"] == "block_timestamp,asc" and seen[0]["event_name"] == "Transfer"
    assert seen[0]["min_block_timestamp"] == "10" and seen[0]["max_block_timestamp"] == "20"
    assert seen[1]["fingerprint"] == "abc" and fp2 is None


async def test_collector_does_not_advance_cursor_on_api_failure(make_harness):
    h = await make_harness()
    before = h.app.collector.stats.confirmed_cursor_ms
    h.clock.advance(minutes=1)
    h.ev("A", "B", 5, h.clock.now())
    h.src.fail_next = 1
    with pytest.raises(TronApiError):
        await h.app.collector.poll_confirmed_once()
    assert h.app.collector.stats.confirmed_cursor_ms == before
    await h.poll()  # API recovered: nothing lost
    assert await h.tx_count() == 1
    assert h.app.collector.stats.confirmed_cursor_ms > before


async def test_collector_does_not_advance_cursor_on_db_failure(make_harness):
    h = await make_harness()
    before = h.app.collector.stats.confirmed_cursor_ms
    h.clock.advance(minutes=1)
    h.ev("A", "B", 5, h.clock.now())
    real = h.app.collector.process

    async def db_down(*a, **k):
        raise ConnectionError("database down")

    h.app.collector.process = db_down
    with pytest.raises(ConnectionError):
        await h.app.collector.poll_confirmed_once()
    assert h.app.collector.stats.confirmed_cursor_ms == before
    h.app.collector.process = real
    await h.poll()
    assert await h.tx_count() == 1


async def test_malformed_event_does_not_stop_processing(make_harness):
    h = await make_harness()
    h.clock.advance(minutes=1)
    h.src.confirmed.append({"transaction_id": "zz", "block_timestamp": datetime_to_ms(h.clock.now()), "contract_address": USDT,
                            "event_name": "Transfer", "result": {"from": "bad"}})
    h.ev("A", "B", 5, h.clock.now())
    await h.poll()
    assert await h.tx_count() == 1
    assert h.app.collector.stats.rejected.get("malformed") == 1


# 29. Telegram failure recovery
async def test_telegram_failure_is_retried_until_delivered(make_harness):
    from tests.test_pipeline import AB, learned

    sink = RecordingSink(fail_times=3)
    h = await learned(make_harness, AB, sink=sink)
    h.clock.advance(minutes=5)
    h.ev("A", "B", 5, h.clock.now())
    await h.poll()  # flush sends until success (failures are rescheduled)
    for _ in range(5):
        await h.app.dispatcher.flush()
    alerts = await h.alerts("TEST_DETECTED")
    assert len(alerts) == 1 and alerts[0].status == "SENT"
    assert kinds(sink.messages).count("TEST") == 1
    assert sink.calls >= 4


async def test_telegram_client_errors_and_rate_limit():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 3}})
        if calls["n"] == 2:
            return httpx.Response(400, json={"ok": False, "description": "Bad Request: can't parse entities"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    client = TelegramClient("TOKEN", transport=httpx.MockTransport(handler))
    sink = TelegramSink(client, "123")
    from app.telegram.alerts import SendError

    with pytest.raises(SendError) as e:
        await sink.send("<b>x</b>")
    assert e.value.retry_after == 3.0
    assert await sink.send("<b>x</b>") == "42"  # falls back to plain text on HTML parse errors
    await client.close()


async def test_dispatcher_prioritises_test_alerts(make_harness):
    from app.database import repository as repo

    h = await make_harness()
    async with h.app.session_factory() as s, s.begin():
        for i, (t, p) in enumerate((("NEW_PATTERN", 4), ("SYSTEM", 5), ("TEST_DETECTED", 0), ("LARGE_FOLLOWUP", 1))):
            await repo.insert_alert(s, dedup_key=f"k{i}", alert_type=t, priority=p, message_text=t, created_at=h.clock.now())
    await h.app.dispatcher.flush()
    assert h.rec.messages == ["TEST_DETECTED", "LARGE_FOLLOWUP", "NEW_PATTERN", "SYSTEM"]


async def test_backfill_resumes_after_interruption(make_harness):
    h = await make_harness()
    start = h.clock.now() - timedelta(days=3)
    for d in range(3):
        h.ev("A", "B", 5, start + timedelta(days=d, hours=1))
    h.src.fail_next = 0
    # interrupt: stop after the first window has been processed
    orig = h.app.collector.process
    count = {"n": 0}

    async def once(events, live):
        count["n"] += 1
        if count["n"] == 2:
            h.app.stop_event.set()
        return await orig(events, live=live)

    h.app.collector.process = once
    await h.backfill(start)
    stored_first = await h.tx_count()
    h.app.stop_event.clear()
    h.app.collector.process = orig
    await h.app.collector.run_backfill(h.app.stop_event, h.app.on_backfill_complete)
    assert stored_first < 3 and await h.tx_count() == 3


async def test_multiple_chat_ids_each_get_every_alert_once():
    from app.telegram.alerts import SendError

    sent: list[str] = []
    fail_once = {"222"}

    def handler(request):
        import json as _json

        chat = str(_json.loads(request.content)["chat_id"])
        if chat in fail_once:
            fail_once.discard(chat)
            return httpx.Response(502, json={"ok": False, "description": "bad gateway"})
        sent.append(chat)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(sent)}})

    s = make_settings("sqlite+aiosqlite://", telegram_bot_token="T", telegram_chat_id=" 111, 222 ,111")
    assert s.telegram_chat_ids == ["111", "222"]
    client = TelegramClient("T", transport=httpx.MockTransport(handler))
    sink = TelegramSink(client, s.telegram_chat_ids)
    with pytest.raises(SendError):
        await sink.send("alert")          # 111 ok, 222 fails
    await sink.send("alert")              # retry: only 222 is sent
    assert sent == ["111", "222"]
    await client.close()
