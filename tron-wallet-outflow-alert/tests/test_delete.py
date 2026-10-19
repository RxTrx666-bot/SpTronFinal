from __future__ import annotations

import pytest

from outflow_alert.commands import CommandHandler
from outflow_alert.deletion import delete_alert
from outflow_alert.storage import now_ms
from tests.fakes import txid

pytestmark = pytest.mark.asyncio

CHATS = ("6371743502", "8972433273")


async def alert_two_chats(build, grid):
    m = build(telegram_chat_ids=CHATS)
    grid.add_transfer(1, 10_000_000, ts=now_ms())
    await m.poll_once()
    await m.deliver_due()
    return m


async def test_message_ids_are_recorded(build, grid, tg):
    m = await alert_two_chats(build, grid)
    sent = m.db.sent_messages_for(txid(1))
    assert [(s.chat_id, s.message_id) for s in sent] == [("6371743502", 1), ("8972433273", 2)]
    assert m.db.recent_alerts()[0].chats == CHATS


async def test_delete_from_one_chat_only(build, grid, tg):
    m = await alert_two_chats(build, grid)
    results = await delete_alert(m.db, m.tg, txid(1), chat_id="8972433273")
    assert [(r.chat_id, r.ok) for r in results] == [("8972433273", True)]
    assert tg.deleted() == [("8972433273", 2)]
    assert m.db.recent_alerts()[0].chats == ("6371743502",)


async def test_delete_everywhere_and_twice(build, grid, tg):
    m = await alert_two_chats(build, grid)
    results = await delete_alert(m.db, m.tg, txid(1))
    assert all(r.ok for r in results) and len(tg.deleted()) == 2
    assert await delete_alert(m.db, m.tg, txid(1)) == []


async def test_too_old_is_reported(build, grid, tg):
    m = await alert_two_chats(build, grid)
    tg.delete_error = "Bad Request: message can't be deleted"
    results = await delete_alert(m.db, m.tg, txid(1))
    assert not any(r.ok for r in results) and "48 hours" in results[0].detail
    assert len(m.db.sent_messages_for(txid(1))) == 2  # still on record


async def test_undelivered_copy_is_cancelled(build, grid, tg):
    m = build(telegram_chat_ids=CHATS)
    grid.add_transfer(1, 10_000_000, ts=now_ms())
    await m.poll_once()  # queued, not delivered
    results = await delete_alert(m.db, m.tg, txid(1), chat_id="8972433273")
    assert results[0].ok and m.db.outbox_count() == 1
    await m.deliver_due()
    assert [s["chat_id"] for s in tg.sent] == ["6371743502"]


async def test_reply_delete_command_removes_for_everyone(build, grid, tg):
    m = await alert_two_chats(build, grid)
    h = CommandHandler(m, m.tg)
    tg.updates = [{"update_id": 1, "message": {
        "message_id": 50, "chat": {"id": 6371743502}, "text": "/delete",
        "reply_to_message": {"message_id": 1, "chat": {"id": 6371743502}}}}]
    await h.poll_updates_once(timeout=0)
    assert sorted(tg.deleted()) == [("6371743502", 1), ("8972433273", 2)]
    assert "8972433273: deleted" in tg.sent[-1]["text"]


async def test_delete_command_by_txid_prefix(build, grid, tg):
    m = await alert_two_chats(build, grid)
    h = CommandHandler(m, m.tg)
    tg.updates = [{"update_id": 1, "message": {"message_id": 50, "chat": {"id": 6371743502},
                                               "text": "/delete ffff"}}]
    await h.poll_updates_once(timeout=0)
    assert tg.deleted() == [] and "No alert" in tg.sent[-1]["text"]
    tg.updates = [{"update_id": 2, "message": {"message_id": 51, "chat": {"id": 6371743502},
                                               "text": f"/delete {txid(1)[:20]}"}}]
    await h.poll_updates_once(timeout=0)
    assert len(tg.deleted()) == 2
