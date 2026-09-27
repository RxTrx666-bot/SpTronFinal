"""80-transaction limit notice: 'Balance negative 🚨 / Fill resources / Run again'."""

import json

import httpx

from app.database import SQLiteRepository
from app.stats import MonitorStats
from app.telegram_bot import AlertDispatcher, TelegramBot, TelegramClient
from app.transaction_parser import parse_trongrid_trc20_record
from app.tron_client import TronClient
from app.tx_limit import TxLimitTracker
from tests.helpers import OTHER, WALLET, BLOCK_TS, make_processor, make_settings, run, trongrid_record


class TelegramRecorder:
    def __init__(self):
        self.texts = []

    def __call__(self, request):
        self.texts.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})


async def setup(threshold="80", repo=None, recorder=None):
    settings = make_settings(TX_LIMIT_THRESHOLD=threshold, ALERT_DIRECTIONS="OUTGOING")
    if repo is None:
        repo = SQLiteRepository(":memory:")
        await repo.init()
    recorder = recorder or TelegramRecorder()
    client = TelegramClient("123:SECRET", transport=httpx.MockTransport(recorder))
    stats = MonitorStats()
    dispatcher = AlertDispatcher(settings, client, repo, stats)
    tracker = TxLimitTracker(repo, settings.tx_limit_threshold, dispatcher)
    dispatcher.limit_tracker = tracker
    processor, _, _, _ = await make_processor(settings, repo=repo)
    processor.alerts = dispatcher
    processor.limit_tracker = tracker
    return settings, repo, dispatcher, tracker, processor, recorder, client


async def send_outgoing(processor, start, count, backfill=False):
    for n in range(start, start + count):
        t = parse_trongrid_trc20_record(trongrid_record(n, sender=WALLET, recipient=OTHER, ts=BLOCK_TS + n))
        await processor.process(t, backfill=backfill)


def is_limit_notice(text):
    return text.startswith("Balance negative 🚨\nFill resources\nRun again")


def test_default_threshold_is_80():
    assert make_settings().tx_limit_threshold == 80


def test_notice_sent_once_at_80th_transaction_right_after_its_alert():
    async def go():
        _, repo, dispatcher, tracker, processor, rec, _ = await setup()
        await send_outgoing(processor, 1, 79)
        await dispatcher.drain()
        assert len(rec.texts) == 79 and not any(is_limit_notice(t) for t in rec.texts)

        await send_outgoing(processor, 80, 1)  # the 80th
        await dispatcher.drain()
        assert "USDT TRANSACTION DETECTED" in rec.texts[-2]
        assert is_limit_notice(rec.texts[-1]) and "80/80" in rec.texts[-1]

        await send_outgoing(processor, 81, 5)  # alerts continue, no repeated notice
        await dispatcher.drain()
        assert len(rec.texts) == 86
        assert sum(is_limit_notice(t) for t in rec.texts) == 1
    run(go())


def test_reset_starts_new_cycle_and_notifies_again_at_next_80():
    async def go():
        _, repo, dispatcher, tracker, processor, rec, _ = await setup(threshold="3")
        await send_outgoing(processor, 1, 4)
        await dispatcher.drain()
        assert sum(is_limit_notice(t) for t in rec.texts) == 1
        assert await tracker.reset() == 2
        assert await tracker.count() == 0
        await send_outgoing(processor, 10, 2)
        await dispatcher.drain()
        assert sum(is_limit_notice(t) for t in rec.texts) == 1
        await send_outgoing(processor, 20, 1)
        await dispatcher.drain()
        assert sum(is_limit_notice(t) for t in rec.texts) == 2
    run(go())


def test_backfill_and_incoming_are_not_counted():
    async def go():
        _, repo, dispatcher, tracker, processor, rec, _ = await setup(threshold="2")
        await send_outgoing(processor, 1, 5, backfill=True)
        for n in (30, 31, 32):  # incoming: filtered out entirely (outgoing-only)
            await processor.process(parse_trongrid_trc20_record(trongrid_record(n, sender=OTHER, recipient=WALLET)))
        assert await tracker.count() == 0
        await dispatcher.drain()
        assert not any(is_limit_notice(t) for t in rec.texts)
    run(go())


def test_counter_and_notified_flag_survive_restart(tmp_path):
    db = str(tmp_path / "m.db")

    async def first():
        repo = SQLiteRepository(db)
        await repo.init()
        _, _, dispatcher, tracker, processor, rec, _ = await setup(threshold="3", repo=repo)
        await send_outgoing(processor, 1, 3)
        await dispatcher.drain()
        await repo.close()
        return rec.texts

    async def second():
        repo = SQLiteRepository(db)
        await repo.init()
        _, _, dispatcher, tracker, processor, rec, _ = await setup(threshold="3", repo=repo)
        assert await tracker.count() == 3
        assert await tracker.check() is False  # already notified this cycle -> no duplicate
        await send_outgoing(processor, 4, 1)
        await dispatcher.drain()
        await repo.close()
        return rec.texts

    assert sum(is_limit_notice(t) for t in run(first())) == 1
    assert not any(is_limit_notice(t) for t in run(second()))


def test_notice_resent_on_startup_if_crash_before_delivery(tmp_path):
    async def go():
        _, repo, dispatcher, tracker, processor, rec, _ = await setup(threshold="2")
        await send_outgoing(processor, 1, 2)
        # simulate crash: queue lost before delivery -> new dispatcher on "restart"
        _, _, dispatcher2, tracker2, _, rec2, _ = await setup(threshold="2", repo=repo)
        await dispatcher2.load_pending()
        assert await tracker2.check() is True
        await dispatcher2.drain()
        assert sum(is_limit_notice(t) for t in rec2.texts) == 1
    run(go())


def test_threshold_zero_disables():
    async def go():
        _, repo, dispatcher, tracker, processor, rec, _ = await setup(threshold="0")
        await send_outgoing(processor, 1, 100)
        await dispatcher.drain()
        assert not any(is_limit_notice(t) for t in rec.texts)
    run(go())


def test_reset_command_and_status_counter():
    async def go():
        settings, repo, dispatcher, tracker, processor, rec, client = await setup(threshold="80")
        await send_outgoing(processor, 1, 5)
        tron = TronClient("https://x", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
        bot = TelegramBot(settings, client, repo, MonitorStats(), tron, tracker)
        update = lambda text: {"update_id": 1, "message": {"chat": {"id": 1001}, "text": text}}
        assert "Transaction Counter:\n🔢 5/80" in await bot.handle_update(update("/status"))
        reply = await bot.handle_update(update("/reset"))
        assert "Counter reset" in reply and "0/80" in reply
        assert "Transaction Counter:\n🔢 0/80" in await bot.handle_update(update("/status"))
        assert "/reset" in await bot.handle_update(update("/help"))
        # unauthorized users cannot reset
        rec.texts.clear()
        await bot.handle_update({"update_id": 2, "message": {"chat": {"id": 999}, "text": "/reset"}})
        assert rec.texts == ["⛔ Unauthorized. This bot is private."] and await tracker.cycle() == 2
    run(go())
