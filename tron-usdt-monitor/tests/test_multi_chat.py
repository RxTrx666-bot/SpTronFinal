"""Several admin chat ids (TELEGRAM_ADMIN_CHAT_ID=a,b)."""

import json

import httpx
import pytest

from app.config import ConfigError
from app.stats import MonitorStats
from app.telegram_bot import AlertDispatcher, TelegramBot, TelegramClient
from app.transaction_parser import parse_trongrid_trc20_record
from app.tron_client import TronClient
from tests.helpers import OTHER, WALLET, make_processor, make_settings, run, trongrid_record, tx_hash

IDS = "8020903132, 8972433273"


async def _nosleep(_):
    return None


class Telegram:
    def __init__(self, blocked=(), flaky=()):
        self.sent = []  # (chat_id, text)
        self.blocked = set(blocked)
        self.flaky = set(flaky)

    def __call__(self, request):
        body = json.loads(request.content)
        chat = body["chat_id"]
        if chat in self.blocked:
            return httpx.Response(403, json={"ok": False, "error_code": 403,
                                             "description": "Forbidden: bot can't initiate conversation with a user"})
        if chat in self.flaky:
            self.flaky.discard(chat)
            return httpx.Response(502, json={"ok": False, "error_code": 502, "description": "Bad Gateway"})
        self.sent.append((chat, body["text"]))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})


def test_config_parses_multiple_ids():
    s = make_settings(TELEGRAM_ADMIN_CHAT_ID=IDS)
    assert s.telegram_admin_chat_ids == (8020903132, 8972433273)
    assert make_settings(TELEGRAM_ADMIN_CHAT_ID="8020903132").telegram_admin_chat_ids == (8020903132,)
    assert make_settings(TELEGRAM_ADMIN_CHAT_ID="5,5").telegram_admin_chat_ids == (5,)
    with pytest.raises(ConfigError):
        make_settings(TELEGRAM_ADMIN_CHAT_ID="8020903132,abc")


async def _deliver_one(tg, **overrides):
    settings = make_settings(TELEGRAM_ADMIN_CHAT_ID=IDS, ALERT_DIRECTIONS="OUTGOING", **overrides)
    processor, repo, _, stats = await make_processor(settings)
    await processor.process(parse_trongrid_trc20_record(
        trongrid_record(1, sender=WALLET, recipient=OTHER, value="1000087")))
    d = AlertDispatcher(settings, TelegramClient("1:X", transport=httpx.MockTransport(tg)), repo, stats,
                        sleep=_nosleep)
    await d.load_pending()
    await d.drain()
    return repo


def test_every_chat_gets_wallet_created_then_alert():
    async def go():
        tg = Telegram()
        repo = await _deliver_one(tg)
        for chat in (8020903132, 8972433273):
            texts = [t for c, t in tg.sent if c == chat]
            assert len(texts) == 2 and "WALLET CREATED" in texts[0] and "TRANSACTION DETECTED" in texts[1]
        assert (await repo.get_transaction(tx_hash(1))).alert_status == "sent"
    run(go())


def test_chat_that_never_pressed_start_does_not_block_the_other():
    async def go():
        tg = Telegram(blocked={8972433273})
        repo = await _deliver_one(tg)
        assert [c for c, _ in tg.sent] == [8020903132, 8020903132]
        assert (await repo.get_transaction(tx_hash(1))).alert_status == "sent"
    run(go())


def test_temporary_failure_is_retried_without_duplicating_other_chat():
    async def go():
        tg = Telegram(flaky={8972433273})
        await _deliver_one(tg)
        assert sum(1 for c, _ in tg.sent if c == 8020903132) == 2  # not re-sent while retrying the other
        assert sum(1 for c, _ in tg.sent if c == 8972433273) == 2
    run(go())


def test_both_admins_can_use_commands_others_rejected():
    async def go():
        settings = make_settings(TELEGRAM_ADMIN_CHAT_ID=IDS)
        _, repo, _, _ = await make_processor(settings)
        tg = Telegram()
        tron = TronClient("https://x", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
        bot = TelegramBot(settings, TelegramClient("1:X", transport=httpx.MockTransport(tg)), repo,
                          MonitorStats(), tron)
        upd = lambda chat, text: {"update_id": 1, "message": {"chat": {"id": chat}, "text": text}}
        assert "MONITORED WALLET" in await bot.handle_update(upd(8020903132, "/wallet"))
        assert "MONITORED WALLET" in await bot.handle_update(upd(8972433273, "/wallet"))
        assert await bot.handle_update(upd(1234, "/wallet")) is None
        await bot.notify_admin("hello")
        assert {c for c, t in tg.sent if t == "hello"} == {8020903132, 8972433273}
    run(go())
