from __future__ import annotations

import httpx
import pytest

from outflow_alert.monitor import StateMismatch
from outflow_alert.storage import now_ms
from outflow_alert.tron_client import TronClient
from outflow_alert.verifier import TokenCheckError, verify_token
from tests.fakes import FAKE_TOKEN, OTHER, USDT, WALLET, FakeTronGrid, txid

pytestmark = pytest.mark.asyncio


async def cycle(monitor):
    await monitor.poll_once()
    await monitor.deliver_due()


async def test_outgoing_matching_transfer_alerts_once(build, grid, tg):
    m = build()
    ts = now_ms()
    grid.add_transfer(1, 1_010_000, ts=ts)
    await cycle(m)
    assert len(tg.sent) == 1
    msg = tg.sent[0]
    assert msg["chat_id"] == "-1001" and msg["parse_mode"] == "HTML"
    assert "1.01 USDT" in msg["text"] and OTHER in msg["text"] and txid(1)[:16] in msg["text"]
    # Re-scanning the lookback window must not produce duplicates.
    await cycle(m)
    await cycle(m)
    assert len(tg.sent) == 1
    assert m.db.verified_outcome(txid(1)) == "alerted"


async def test_filters(build, grid, tg):
    m = build()
    ts = now_ms()
    grid.add_transfer(1, 10_000_100, ts=ts)                       # within tolerance
    grid.add_transfer(2, 10_000_101, ts=ts + 1)                   # outside tolerance
    grid.add_transfer(3, 20_000_000, ts=ts + 2, sender=OTHER, recipient=WALLET)  # incoming
    grid.add_transfer(4, 30_000_000, ts=ts + 3, receipt="REVERT")  # failed receipt
    grid.add_transfer(5, 20_000_000, ts=ts + 4, log_contract=FAKE_TOKEN)  # log from another contract
    grid.add_transfer(6, 20_000_000, ts=ts + 5, contract=FAKE_TOKEN)      # listing for another token
    await cycle(m)
    texts = [s["text"] for s in tg.sent]
    assert len(texts) == 1 and "10.0001 USDT" in texts[0]
    assert m.db.verified_outcome(txid(2)) is None          # never even verified (pre-filter)
    assert m.db.verified_outcome(txid(4)) == "rejected"
    assert m.db.verified_outcome(txid(5)) == "rejected"
    assert m.db.verified_outcome(txid(6)) is None


async def test_listing_amount_lies_but_log_is_authoritative(build, grid, tg):
    m = build()
    tid = grid.add_transfer(1, 10_000_000, ts=now_ms())
    grid.infos[tid]["log"][0]["data"] = f"{9_000_000:064x}"
    await cycle(m)
    assert tg.sent == []
    assert m.db.verified_outcome(tid) == "rejected"


async def test_pending_tx_is_retried_until_in_block(build, grid, tg):
    m = build()
    tid = grid.add_transfer(1, 20_000_000, ts=now_ms())
    info = grid.infos.pop(tid)
    await cycle(m)
    assert tg.sent == [] and m.db.pending_count() == 1
    grid.infos[tid] = info
    m.db._db.execute("UPDATE candidates SET next_attempt_at = 0")
    await cycle(m)
    assert len(tg.sent) == 1 and m.db.pending_count() == 0


async def test_pagination_and_cursor(build, grid, tg):
    m = build(page_limit=2)
    base = now_ms()
    for i in range(7):
        grid.add_transfer(i + 1, 30_000_000, ts=base + i * 1000)
    await cycle(m)
    assert len(tg.sent) == 7
    assert m.db.get_cursor() == base + 6000


async def test_restart_resumes_without_missing_or_duplicating(build, grid, tg, tmp_path):
    db = str(tmp_path / "restart.db")
    m1 = build(db)
    t0 = now_ms()
    grid.add_transfer(1, 10_000_000, ts=t0)
    await cycle(m1)
    assert len(tg.sent) == 1
    m1.db.close()

    # While "down": a new transfer arrives far past the lookback window relative to now.
    grid.add_transfer(2, 20_000_000, ts=t0 + 5_000)
    m2 = build(db, lookback_seconds=0)
    await cycle(m2)
    assert len(tg.sent) == 2
    assert "20 USDT" in tg.sent[1]["text"]


async def test_outbox_survives_telegram_outage(build, grid, tg):
    m = build()
    grid.add_transfer(1, 1_010_000, ts=now_ms())
    tg.fail_next = [httpx.Response(502, json={"ok": False, "description": "bad gateway"})]
    await cycle(m)
    assert tg.sent == [] and m.db.outbox_count() == 1
    m.db._db.execute("UPDATE outbox SET next_attempt_at = 0")
    await m.deliver_due()
    assert len(tg.sent) == 1 and m.db.outbox_count() == 0


async def test_tron_outage_does_not_advance_cursor(build, grid, tg):
    m = build()
    cursor = m.db.get_cursor()
    grid.fail_listing = 1
    grid.add_transfer(1, 10_000_000, ts=cursor + 1000)
    await cycle(m)
    assert tg.sent == [] and m.db.get_cursor() == cursor
    await cycle(m)
    assert len(tg.sent) == 1


async def test_multiple_chats(build, grid, tg):
    m = build(telegram_chat_ids=("-1", "-2"))
    grid.add_transfer(1, 10_000_000, ts=now_ms())
    await cycle(m)
    assert sorted(s["chat_id"] for s in tg.sent) == ["-1", "-2"]


async def test_db_bound_to_wallet(build, tmp_path):
    db = str(tmp_path / "bound.db")
    m = build(db)
    m.db.close()
    with pytest.raises(StateMismatch):
        build(db, wallet=OTHER)


async def test_api_key_header_sent(build, grid):
    m = build()
    await m.poll_once()
    assert all(h.get("tron-pro-api-key") == "test-key" for h in grid.headers_seen)


async def test_token_verification():
    grid = FakeTronGrid()
    client = TronClient("https://api.trongrid.io", "k", transport=httpx.MockTransport(grid.handler))
    await verify_token(client, USDT, WALLET, symbol="USDT", decimals=6)
    grid.symbol = "USDX"
    with pytest.raises(TokenCheckError, match="symbol"):
        await verify_token(client, USDT, WALLET, symbol="USDT", decimals=6)
    grid.symbol, grid.decimals = "USDT", 18
    with pytest.raises(TokenCheckError, match="decimals"):
        await verify_token(client, USDT, WALLET, symbol="USDT", decimals=6)
    await client.close()


async def test_run_loops_end_to_end(build, grid, tg):
    import asyncio

    m = build(poll_interval_seconds=0.5)
    task = asyncio.create_task(m.run())
    await asyncio.sleep(0.2)
    grid.add_transfer(1, 30_000_000, ts=now_ms())
    for _ in range(50):
        if tg.sent:
            break
        await asyncio.sleep(0.1)
    m.stop()
    await asyncio.wait_for(task, timeout=5)
    assert len(tg.sent) == 1 and "30 USDT" in tg.sent[0]["text"]
