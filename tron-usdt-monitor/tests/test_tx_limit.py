"""Transaction limit (default 150): notice 'Balance negative 🚨 / Fill resources / Run again',
pause until ▶️ Start / /start, then count again from 0."""

import json

import httpx

from app.database import SQLiteRepository
from app.stats import MonitorStats
from app.telegram_bot import RESUME_CALLBACK, AlertDispatcher, TelegramBot, TelegramClient
from app.transaction_parser import parse_trongrid_trc20_record
from app.tron_client import TronClient
from app.tron_monitor import STATE_ACCOUNT_CURSOR, AccountMonitor
from app.tx_limit import TxLimitTracker
from tests.helpers import OTHER, WALLET, BLOCK_TS, make_processor, make_settings, run, trongrid_record, tx_hash

ADMIN = 1001


class TelegramRecorder:
    def __init__(self):
        self.messages = []  # (method, body)

    @property
    def texts(self):
        return [b["text"] for m, b in self.messages if m == "sendMessage"]

    def __call__(self, request):
        method = request.url.path.rsplit("/", 1)[-1]
        self.messages.append((method, json.loads(request.content) if request.content else {}))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})


async def setup(threshold="150", repo=None, recorder=None, **env):
    settings = make_settings(TX_LIMIT_THRESHOLD=threshold, ALERT_DIRECTIONS="OUTGOING",
                             WALLET_CREATED_NOTICE="false", **env)
    if repo is None:
        repo = SQLiteRepository(":memory:")
        await repo.init()
    recorder = recorder or TelegramRecorder()
    client = TelegramClient("123:SECRET", transport=httpx.MockTransport(recorder))
    stats = MonitorStats()
    dispatcher = AlertDispatcher(settings, client, repo, stats)
    tracker = TxLimitTracker(repo, settings.tx_limit_threshold, dispatcher, settings.pause_on_limit)
    await tracker.load()
    dispatcher.limit_tracker = tracker
    processor, _, _, _ = await make_processor(settings, repo=repo)
    processor.alerts = dispatcher
    processor.limit_tracker = tracker
    tron = TronClient("https://x", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    bot = TelegramBot(settings, client, repo, stats, tron, tracker)
    return settings, repo, dispatcher, tracker, processor, recorder, bot


async def send_outgoing(processor, start, count, backfill=False):
    results = []
    for n in range(start, start + count):
        t = parse_trongrid_trc20_record(trongrid_record(n, sender=WALLET, recipient=OTHER, value="1000050",
                                                        ts=BLOCK_TS + n))
        results.append(await processor.process(t, backfill=backfill))
    return results


def is_limit_notice(text):
    return text.startswith("Balance negative 🚨\nFill resources\nRun again")


def alerts(texts):
    return [t for t in texts if "USDT TRANSACTION DETECTED" in t]


def cmd(text, chat=ADMIN):
    return {"update_id": 1, "message": {"chat": {"id": chat}, "text": text}}


def press_start(chat=ADMIN):
    return {"update_id": 2, "callback_query": {"id": "cb1", "from": {"id": chat}, "data": RESUME_CALLBACK,
                                               "message": {"chat": {"id": chat}}}}


def test_default_threshold_is_150_and_pause_enabled():
    s = make_settings()
    assert s.tx_limit_threshold == 150 and s.pause_on_limit is True


def test_at_150_notice_with_start_button_then_paused():
    async def go():
        _, repo, dispatcher, tracker, processor, rec, _ = await setup()
        await send_outgoing(processor, 1, 149)
        await dispatcher.drain()
        assert len(alerts(rec.texts)) == 149 and not any(is_limit_notice(t) for t in rec.texts)
        assert not tracker.paused

        assert await send_outgoing(processor, 150, 1) == [True]  # the 150th is still alerted
        await dispatcher.drain()
        assert "USDT TRANSACTION DETECTED" in rec.texts[-2]
        notice = rec.texts[-1]
        assert is_limit_notice(notice) and "150/150" in notice and "PAUSED" in notice
        body = [b for m, b in rec.messages if m == "sendMessage"][-1]
        assert body["reply_markup"]["inline_keyboard"][0][0] == {"text": "▶️ Start", "callback_data": "resume"}
        assert tracker.paused

        # while paused: nothing is detected or alerted
        assert await send_outgoing(processor, 151, 5) == [False] * 5
        await dispatcher.drain()
        assert len(alerts(rec.texts)) == 150 and sum(is_limit_notice(t) for t in rec.texts) == 1
    run(go())


def test_start_button_resumes_and_counts_again_from_zero():
    async def go():
        _, repo, dispatcher, tracker, processor, rec, bot = await setup(threshold="3")
        await send_outgoing(processor, 1, 3)
        await dispatcher.drain()
        assert tracker.paused
        reply = await bot.handle_update(press_start())
        assert "Monitoring resumed" in reply and "0/3" in reply
        assert ("answerCallbackQuery", {"callback_query_id": "cb1", "text": ""}) in rec.messages
        assert not tracker.paused and await tracker.count() == 0
        # counting again: 3 more -> paused again with a second notice
        assert await send_outgoing(processor, 10, 3) == [True] * 3
        await dispatcher.drain()
        assert tracker.paused and sum(is_limit_notice(t) for t in rec.texts) == 2
    run(go())


def test_start_command_resumes_and_all_admins_are_told():
    async def go():
        ids = "1001,2002"
        _, repo, dispatcher, tracker, processor, rec, bot = await setup(threshold="2", TELEGRAM_ADMIN_CHAT_ID=ids)
        await send_outgoing(processor, 1, 2)
        await dispatcher.drain()
        assert tracker.paused
        rec.messages.clear()
        await bot.handle_update(cmd("/start", chat=2002))
        resumed = [b["chat_id"] for m, b in rec.messages if m == "sendMessage" and "resumed" in b["text"]]
        assert sorted(resumed) == [1001, 2002]  # both admins informed, no duplicate to the sender
        assert not tracker.paused
        # /start when NOT paused just shows the intro
        assert "running" in await bot.handle_update(cmd("/start"))
    run(go())


def test_unauthorized_cannot_press_start():
    async def go():
        _, repo, dispatcher, tracker, processor, rec, bot = await setup(threshold="1")
        await send_outgoing(processor, 1, 1)
        assert tracker.paused
        assert await bot.handle_update(press_start(chat=999)) is None
        assert await bot.handle_update(cmd("/start", chat=999)) is None
        assert tracker.paused
    run(go())


def test_status_and_wallet_show_paused():
    async def go():
        _, repo, dispatcher, tracker, processor, rec, bot = await setup(threshold="1")
        await send_outgoing(processor, 1, 1)
        bot.stats.paused = tracker.paused
        assert "⏸️ PAUSED" in await bot.handle_update(cmd("/status"))
        assert "⏸️ PAUSED" in await bot.handle_update(cmd("/wallet"))
    run(go())


def test_pause_and_count_survive_restart(tmp_path):
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
        assert tracker.paused and await tracker.count() == 3
        assert await tracker.check() is False  # already notified -> no duplicate notice
        assert await send_outgoing(processor, 4, 1) == [False]  # still paused after restart
        await dispatcher.drain()
        await repo.close()
        return rec.texts

    assert sum(is_limit_notice(t) for t in run(first())) == 1
    assert run(second()) == []


def test_changing_threshold_counts_from_now(tmp_path):
    db = str(tmp_path / "m.db")

    async def go():
        repo = SQLiteRepository(db)
        await repo.init()
        _, _, _, tracker, processor, _, _ = await setup(threshold="80", repo=repo)
        await send_outgoing(processor, 1, 30)
        assert await tracker.count() == 30
        _, _, _, tracker2, _, _, _ = await setup(threshold="150", repo=repo)  # redeploy with 150
        assert await tracker2.count() == 0
        _, _, _, tracker3, _, _, _ = await setup(threshold="150", repo=repo)  # plain restart keeps count
        assert await tracker3.count() == 0 and await tracker3.cycle() == await tracker2.cycle()
        await repo.close()
    run(go())


def test_backfill_not_counted_and_zero_disables():
    async def go():
        _, repo, dispatcher, tracker, processor, rec, _ = await setup(threshold="2")
        await send_outgoing(processor, 1, 5, backfill=True)
        assert await tracker.count() == 0 and not tracker.paused
        _, repo2, d2, t2, p2, rec2, _ = await setup(threshold="0")
        assert await send_outgoing(p2, 1, 200) == [True] * 200
        await d2.drain()
        assert not t2.paused and not any(is_limit_notice(t) for t in rec2.texts)
    run(go())


def test_notice_resent_on_startup_if_crash_before_delivery():
    async def go():
        _, repo, dispatcher, tracker, processor, rec, _ = await setup(threshold="2")
        await send_outgoing(processor, 1, 2)  # notice queued but never delivered ("crash")
        _, _, dispatcher2, tracker2, _, rec2, _ = await setup(threshold="2", repo=repo)
        await dispatcher2.load_pending()
        assert tracker2.paused and await tracker2.check() is True
        await dispatcher2.drain()
        assert sum(is_limit_notice(t) for t in rec2.texts) == 1
    run(go())


def test_monitor_idles_while_paused_and_reanchors_to_now_on_resume():
    from tests.test_monitor import FakeTron

    async def go():
        settings, repo, dispatcher, tracker, processor, rec, bot = await setup(
            threshold="1", VERIFY_EVENT_LOG="false")
        tron = FakeTron()
        mon = AccountMonitor(settings, tron, repo, processor, processor.stats)
        await mon.initialize()
        mon.stats.initialized = True
        tron.records.append(trongrid_record(1, sender=WALLET, recipient=OTHER, value="1000000", ts=BLOCK_TS + 1000))
        await mon.poll_once()
        assert tracker.paused
        # a transfer happens while paused (e.g. during refilling) -> must NOT alert after resume
        tron.records.append(trongrid_record(2, sender=WALLET, recipient=OTHER, value="1000000", ts=BLOCK_TS + 5000))
        tron.head = (70_000_100, BLOCK_TS + 60_000)  # "now" when Start is pressed
        await tracker.resume()
        # run one loop iteration: re-anchor + initialize + poll
        import asyncio
        stop = asyncio.Event()

        async def one_iteration(_):
            stop.set()

        mon._sleep = one_iteration
        await mon.run(stop)
        assert await repo.get_state(STATE_ACCOUNT_CURSOR) == str(BLOCK_TS + 60_000)
        assert not await repo.exists(tx_hash(2))
        # new activity after resume is detected
        tron.records.append(trongrid_record(3, sender=WALLET, recipient=OTHER, value="1000000", ts=BLOCK_TS + 61_000))
        await mon.poll_once()
        assert await repo.exists(tx_hash(3))
    run(go())
