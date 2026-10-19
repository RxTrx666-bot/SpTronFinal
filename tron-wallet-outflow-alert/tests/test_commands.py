from __future__ import annotations

import dataclasses

import pytest

from outflow_alert.commands import CommandHandler
from outflow_alert.storage import now_ms
from outflow_alert.telegram import StatusSnapshot, format_status
from tests.fakes import USDT, WALLET

EXPECTED_PAUSED = """📊 BOT STATUS
━━━━━━━━━━━━━━━━━━
🚦 Status: ⏸️ PAUSED
📡 Monitoring: ⏸️ PAUSED
━━━━━━━━━━━━━━━━━━
👛 Wallet:
TWkvffFDMsqbmTLkMHMABmw452Hyq98cdn
🌐 Network: TRON Mainnet
💎 Token: USDT TRC-20
📜 Contract:
TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t
━━━━━━━━━━━━━━━━━━
⚡ API latency: 20 ms (avg 123 ms)
🛰️ Mode: account · poll 2s · fast
🕒 Last poll: never
🔁 Transfers checked: 0"""


def snapshot(**kw) -> StatusSnapshot:
    base = dict(paused=True, poll_error=None, wallet=WALLET, contract=USDT, last_latency_ms=20.4,
                avg_latency_ms=122.6, poll_interval_seconds=2.0, confirmation_mode="fast",
                last_poll_ms=None, transfers_checked=0, now_ms=1_760_000_000_000)
    base.update(kw)
    return StatusSnapshot(**base)


def test_status_matches_requested_layout_exactly():
    assert format_status(snapshot()) == EXPECTED_PAUSED


def test_status_running_variants():
    t = 1_760_000_000_000
    text = format_status(snapshot(paused=False, last_poll_ms=t - 3000, transfers_checked=1234, now_ms=t))
    assert "🚦 Status: ▶️ RUNNING" in text and "📡 Monitoring: 🟢 ACTIVE" in text
    assert "🕒 Last poll: 2025-10-09 08:53:17 UTC (3s ago)" in text
    assert "🔁 Transfers checked: 1,234" in text
    err = format_status(snapshot(paused=False, last_poll_ms=t, poll_error="TronGrid HTTP 503", now_ms=t))
    assert "📡 Monitoring: 🔴 ERROR" in err and "TronGrid HTTP 503" in err
    assert "⚡ API latency: n/a" in format_status(snapshot(last_latency_ms=None, avg_latency_ms=None))


def msg(update_id: int, text: str, chat_id: int = -1001) -> dict:
    return {"update_id": update_id, "message": {"message_id": update_id, "chat": {"id": chat_id}, "text": text}}


@pytest.mark.asyncio
async def test_status_command_and_buttons(build, grid, tg):
    m = build()
    await m.poll_once()  # records latency + last poll
    h = CommandHandler(m, m.tg)
    tg.updates = [msg(1, "/status@outflow_bot")]
    await h.poll_updates_once(timeout=0)
    text = tg.sent[-1]["text"]
    assert text.startswith("📊 BOT STATUS") and "▶️ RUNNING" in text and "🟢 ACTIVE" in text
    assert "⚡ API latency: " in text and "n/a" not in text
    buttons = tg.sent[-1]["reply_markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == ["pause", "status"]

    # Press "Pause" on the card.
    tg.updates = [{"update_id": 2, "callback_query": {"id": "q1", "data": "pause",
                                                       "message": {"message_id": 1, "chat": {"id": -1001}}}}]
    await h.poll_updates_once(timeout=0)
    assert m.paused
    edits = [b for meth, b in tg.calls if meth == "editMessageText"]
    assert "🚦 Status: ⏸️ PAUSED" in edits[-1]["text"]
    assert edits[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "resume"
    assert m.db.get("telegram_offset") == "3"


@pytest.mark.asyncio
async def test_pause_stops_polling_and_resume_skips_paused_period(build, grid, tg):
    m = build()
    m.pause()
    grid.add_transfer(1, 10_000_000, ts=now_ms() - 1000)
    await m.poll_once()
    await m.deliver_due()
    assert tg.sent == [] and m.last_poll_ms is None
    assert not any(path.endswith("trc20") for _, path in grid.calls)
    m.resume()
    m.s = dataclasses.replace(m.s, lookback_seconds=0)
    await m.poll_once()
    await m.deliver_due()
    assert tg.sent == []  # made while paused -> not alerted
    grid.add_transfer(2, 10_000_000, ts=now_ms() + 1000)
    await m.poll_once()
    await m.deliver_due()
    assert len(tg.sent) == 1


@pytest.mark.asyncio
async def test_pause_survives_restart(build, tmp_path):
    db = str(tmp_path / "p.db")
    m = build(db)
    m.pause()
    m.db.close()
    assert build(db).paused


@pytest.mark.asyncio
async def test_unauthorised_chat_is_ignored(build, tg):
    m = build()
    h = CommandHandler(m, m.tg)
    tg.updates = [msg(1, "/pause", chat_id=999)]
    await h.poll_updates_once(timeout=0)
    assert tg.sent == [] and not m.paused


@pytest.mark.asyncio
async def test_pause_resume_commands(build, tg):
    m = build()
    h = CommandHandler(m, m.tg)
    tg.updates = [msg(1, "/pause")]
    await h.poll_updates_once(timeout=0)
    assert m.paused and tg.sent[-1]["text"].startswith("⏸️ Monitoring paused.")
    tg.updates = [msg(2, "/resume")]
    await h.poll_updates_once(timeout=0)
    assert not m.paused and "▶️ RUNNING" in tg.sent[-1]["text"]


@pytest.mark.asyncio
async def test_transfers_checked_counts_each_transfer_once(build, grid):
    m = build()
    ts = now_ms()
    grid.add_transfer(1, 10_000_000, ts=ts)
    grid.add_transfer(2, 5_000_000, ts=ts + 1)  # not in filter, still checked
    for _ in range(3):
        await m.poll_once()
    assert m.db.transfers_checked() == 2

