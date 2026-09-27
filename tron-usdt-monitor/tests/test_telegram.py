import json

import httpx

from app.formatting import build_alert_message, build_status_message, build_wallet_message
from app.stats import MonitorStats
from app.telegram_bot import TelegramBot, TelegramClient
from app.timeutil import format_utc
from app.transaction_parser import parse_trongrid_trc20_record
from app.tron_client import TronClient
from tests.helpers import OTHER, WALLET, make_processor, make_settings, run, trongrid_record, tx_hash

TS = 1_790_543_922_000  # 2026-09-27 21:18:42 UTC


def test_timestamp_format():
    assert format_utc(TS) == "2026-09-27 21:18:42 UTC"
    assert format_utc(TS + 1120, with_millis=True) == "2026-09-27 21:18:43.120 UTC"


def test_alert_message_outgoing_and_incoming():
    async def go():
        settings = make_settings(TIMEZONE="Europe/Berlin")
        processor, repo, _, _ = await make_processor(settings, clock=lambda: TS + 1120)
        await processor.process(parse_trongrid_trc20_record(
            trongrid_record(1, sender=WALLET, recipient=OTHER, value="1100000", ts=TS)))
        await processor.process(parse_trongrid_trc20_record(
            trongrid_record(2, sender=OTHER, recipient=WALLET, value="1050000", ts=TS)))
        out = build_alert_message(await repo.get_transaction(tx_hash(1)), settings)
        inc = build_alert_message(await repo.get_transaction(tx_hash(2)), settings)
        assert "🚨 <b>USDT TRANSACTION DETECTED</b>" in out
        assert "Direction: <b>OUTGOING</b>" in out and "Amount: <b>1.100000 USDT</b>" in out
        assert out.index(WALLET) < out.index(OTHER)  # From = wallet
        assert "Direction: <b>INCOMING</b>" in inc and "1.050000 USDT" in inc
        assert inc.index(OTHER) < inc.index(WALLET)  # To = wallet
        assert "Blockchain Time:\n2026-09-27 21:18:42 UTC\n2026-09-27 23:18:42 CEST" in out
        assert "Detected By Bot:\n2026-09-27 21:18:43 UTC" in out
        assert "Detection Latency: 1.120 s" in out
        link = f"https://tronscan.org/#/transaction/{tx_hash(1)}"
        assert f'<a href="{link}">{link}</a>' in out
    run(go())


class Recorder:
    def __init__(self):
        self.sent = []

    def __call__(self, request):
        body = json.loads(request.content) if request.content else {}
        self.sent.append((request.url.path.rsplit("/", 1)[-1], body))
        return httpx.Response(200, json={"ok": True, "result": []})


def make_bot(settings, repo, recorder):
    client = TelegramClient("123:SECRET", transport=httpx.MockTransport(recorder))
    tron = TronClient("https://api.trongrid.io", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    stats = MonitorStats()
    return TelegramBot(settings, client, repo, stats, tron), stats


def update(chat_id, text):
    return {"update_id": 1, "message": {"chat": {"id": chat_id}, "from": {"id": chat_id}, "text": text}}


def test_unauthorized_user_rejected():
    async def go():
        settings = make_settings()
        _, repo, _, _ = await make_processor(settings)
        rec = Recorder()
        bot, _ = make_bot(settings, repo, rec)
        assert await bot.handle_update(update(999, "/status")) is None
        assert len(rec.sent) == 1 and "Unauthorized" in rec.sent[0][1]["text"]
        assert "BOT STATUS" not in rec.sent[0][1]["text"]
    run(go())


def test_admin_commands():
    async def go():
        settings = make_settings()
        processor, repo, _, _ = await make_processor(settings)
        await processor.process(parse_trongrid_trc20_record(trongrid_record(5)))
        rec = Recorder()
        bot, stats = make_bot(settings, repo, rec)
        stats.initialized = True
        stats.poll_succeeded()
        wallet = await bot.handle_update(update(1001, "/wallet"))
        assert "👛 <b>MONITORED WALLET</b>" in wallet and WALLET in wallet
        assert "1.000000 – 1.200000 USDT" in wallet and "🟢 ACTIVE" in wallet
        status = await bot.handle_update(update(1001, "/status@MyBot"))
        assert "Status: 🟢 ONLINE" in status and "TRON Mainnet" in status
        assert "Transactions Detected:\n1 total" in status and "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t" in status
        assert "SECRET" not in status and "TEST-TOKEN" not in status and "49a5e403" not in status
        assert "/wallet" in await bot.handle_update(update(1001, "/help"))
        assert "running" in await bot.handle_update(update(1001, "/start"))
        assert all(chat_id == 1001 for _, body in rec.sent for chat_id in [body["chat_id"]])
    run(go())


def test_status_degraded_when_polls_stale():
    settings = make_settings()
    stats = MonitorStats(initialized=True)
    assert "STARTING" in build_status_message(settings, stats, 0, None, None, None, 0)
    stats.poll_failed("TronApiError: timeout")
    text = build_status_message(settings, stats, 0, None, None, None, 0)
    assert "DEGRADED" in text and "Consecutive API errors: 1" in text
    assert "NOT ACTIVE" in build_wallet_message(settings, stats)
