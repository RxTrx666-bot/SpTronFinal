"""Alert delivery (outbox), Telegram commands, message formatting, health."""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import select

from app.alerts.dispatcher import AlertDispatcher
from app.alerts.formatter import humanize
from app.db.models import Alert
from app.domain import ALERT_PENDING, ALERT_SENDING, ALERT_SENT
from app.health import serve_health
from app.telegram.client import TelegramError
from app.telegram.commands import CommandHandler
from tests.conftest import make_settings
from tests.fakes import T0, WALLET_A, FakeTelegram, addr, usdt

W1 = addr("wallet-1")
X = addr("unrelated-sender")
ADMIN = "1000000001"


async def poll(rt):
    while await rt.stream.poll_once():
        pass


async def make_alert(rt, tron, amount="2500"):
    tron.add(WALLET_A, W1, usdt("0.01"), ts=T0 + 1000)
    big = tron.add(X, W1, usdt(amount), ts=T0 + 1000 + 3 * 3600_000 + 17 * 60_000)
    await poll(rt)
    return big


async def all_alerts(sf):
    async with sf() as s:
        return list((await s.scalars(select(Alert).order_by(Alert.id))).all())


# ------------------------------------------------------------ dispatcher


async def test_alert_sent_exactly_once(rt, sf, tron, settings):
    big = await make_alert(rt, tron)
    tg = FakeTelegram()
    d = AlertDispatcher(settings, sf, tg)
    assert await d.deliver_pending() == 1
    assert await d.deliver_pending() == 0
    await poll(rt)  # re-processing the chain data
    assert await d.deliver_pending() == 0
    assert len(tg.sent) == 1
    chat, text, _ = tg.sent[0]
    assert chat == ADMIN
    for needle in (
        "LARGE USDT TRANSFER DETECTED",
        "Network: TRON",
        "Token: USDT TRC-20",
        "Amount: <b>2,500 USDT</b>",
        W1,
        X,
        WALLET_A,
        big["transaction_id"],
        f"https://tronscan.org/#/transaction/{big['transaction_id']}",
        "3 hours 17 minutes",
        "UTC",
    ):
        assert needle in text, needle
    [a] = await all_alerts(sf)
    assert a.status == ALERT_SENT and a.telegram_message_id == "1" and a.sent_at is not None


async def test_telegram_failure_retried_without_duplicates(rt, sf, tron, settings):
    await make_alert(rt, tron)
    tg = FakeTelegram()
    tg.fail_times = 2
    d = AlertDispatcher(settings, sf, tg)
    for _ in range(2):
        with pytest.raises(TelegramError):
            await d.deliver_pending()
        [a] = await all_alerts(sf)
        assert a.status == ALERT_PENDING and "502" in a.last_error
    assert await d.deliver_pending() == 1
    assert len(tg.sent) == 1
    [a] = await all_alerts(sf)
    assert a.status == ALERT_SENT and a.attempts == 3


async def test_concurrent_dispatchers_send_once(rt, sf, tron, settings):
    await make_alert(rt, tron)
    tg = FakeTelegram()
    d1, d2 = AlertDispatcher(settings, sf, tg), AlertDispatcher(settings, sf, tg)
    await asyncio.gather(d1.deliver_pending(), d2.deliver_pending())
    assert len(tg.sent) == 1


async def test_crash_during_send_is_recovered(rt, sf, tron, settings):
    await make_alert(rt, tron)
    tg = FakeTelegram()
    tg.crash_after_send = True
    with pytest.raises(SystemExit):
        await AlertDispatcher(settings, sf, tg).deliver_pending()
    [a] = await all_alerts(sf)
    assert a.status == ALERT_SENDING
    # restart
    tg2 = FakeTelegram()
    d = AlertDispatcher(settings, sf, tg2)
    assert await d.recover() == 1
    assert await d.deliver_pending() == 1
    assert "Re-sent after a restart" in tg2.sent[0][1]


async def test_run_loop_delivers_on_wake(rt, sf, tron, settings):
    tg = FakeTelegram()
    d = AlertDispatcher(settings, sf, tg)
    rt.processor.on_alert = d.wake
    stop = asyncio.Event()
    task = asyncio.create_task(d.run(stop))
    await asyncio.sleep(0.05)
    await make_alert(rt, tron)
    for _ in range(100):
        if tg.sent:
            break
        await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(task, 2)
    assert len(tg.sent) == 1


async def test_discovery_message_format(sf, tron):
    from app.main import build_runtime

    settings = make_settings(alert_on_discovery=True)
    rt = await build_runtime(settings, sf, tron, now_ms=T0)
    tron.add(WALLET_A, W1, usdt("0.01"), ts=T0 + 1000)
    await poll(rt)
    tg = FakeTelegram()
    await AlertDispatcher(settings, sf, tg).deliver_pending()
    text = tg.sent[0][1]
    assert "NEW WALLET DISCOVERED" in text and W1 in text and "0.01 USDT" in text


def test_humanize():
    assert humanize(3 * 3600 + 17 * 60 + 5) == "3 hours 17 minutes"
    assert humanize(86400 * 2 + 3600) == "2 days 1 hour"
    assert humanize(45) == "45 seconds"
    assert humanize(60) == "1 minute"


# ------------------------------------------------------------ commands


def _msg(text, chat=ADMIN):
    return {"update_id": 1, "message": {"chat": {"id": int(chat)}, "from": {"id": int(chat)}, "text": text}}


async def test_unauthorized_commands_ignored(rt, sf, tron):
    tg = FakeTelegram()
    h = CommandHandler(rt, tg)
    for cmd in ("/status", "/pause", "/wallets", "/stats"):
        await h.handle_update(_msg(cmd, chat="999"))
    assert tg.sent == [] and rt.paused is False


async def test_status_stats_recent(rt, sf, tron):
    await make_alert(rt, tron)
    rt.stream.last_block = 70_123_456
    tg = FakeTelegram()
    h = CommandHandler(rt, tg)
    await h.handle_update(_msg("/status"))
    status = tg.sent[-1][1]
    for needle in (WALLET_A, "Discovered wallets: <b>1</b>", "Currently monitored: <b>1</b>", "Large transfers detected: <b>1</b>", "70123456", "Uptime"):
        assert needle in status, needle
    await h.handle_update(_msg("/stats"))
    stats = tg.sent[-1][1]
    assert "Total alerts: <b>1</b>" in stats and "2,500 USDT" in stats and "Alerts in last 24h" in stats
    await h.handle_update(_msg("/recent"))
    assert "2,500 USDT" in tg.sent[-1][1]
    await h.handle_update(_msg("/help@my_bot"))
    assert "500 USDT" in tg.sent[-1][1]
    for text in (status, stats):
        assert "TEST-TOKEN" not in text and "test-api-key" not in text


async def test_pause_resume_persisted(rt, sf, tron, settings):
    tg = FakeTelegram()
    h = CommandHandler(rt, tg)
    await h.handle_update(_msg("/pause"))
    assert rt.paused
    from app.main import build_runtime

    rt2 = await build_runtime(settings, sf, tron, now_ms=T0)
    assert rt2.paused  # survives restart
    await h.handle_update(_msg("/resume"))
    assert not rt.paused


async def test_wallets_pagination(sf, tron):
    from app.main import build_runtime

    settings = make_settings(wallets_page_size=10)
    rt = await build_runtime(settings, sf, tron, now_ms=T0)
    for i in range(25):
        tron.add(WALLET_A, addr(f"w{i}"), 1, ts=T0 + 1000 + i)
    await poll(rt)
    tg = FakeTelegram()
    h = CommandHandler(rt, tg)
    await h.handle_update(_msg("/wallets"))
    text, markup = tg.sent[-1][1], tg.sent[-1][2]
    assert "(25)" in text and "page 1/3" in text and text.count("<code>") == 10
    assert markup["inline_keyboard"][0] == [{"text": "Next ▶️", "callback_data": "wallets:2"}]
    await h.handle_update(_msg("/wallets 3"))
    assert "page 3/3" in tg.sent[-1][1] and tg.sent[-1][1].count("<code>") == 5
    # inline button
    cb = {"update_id": 2, "callback_query": {"id": "c", "from": {"id": int(ADMIN)}, "data": "wallets:2", "message": {"message_id": 7, "chat": {"id": int(ADMIN)}}}}
    await h.handle_update(cb)
    assert tg.edited and "page 2/3" in tg.edited[-1][1]


# ------------------------------------------------------------ health


async def test_health_endpoint(rt, settings):
    import time

    rt.settings = make_settings(health_port=18087, health_host="127.0.0.1")
    rt.stream.last_poll_at = time.time()
    stop = asyncio.Event()
    task = asyncio.create_task(serve_health(rt, stop))
    await asyncio.sleep(0.1)
    reader, writer = await asyncio.open_connection("127.0.0.1", 18087)
    writer.write(b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
    await writer.drain()
    raw = await reader.read()
    writer.close()
    stop.set()
    await task
    head, body = raw.split(b"\r\n\r\n", 1)
    assert b"200 OK" in head
    data = json.loads(body)
    assert data["status"] == "ok" and data["db_ok"] is True
