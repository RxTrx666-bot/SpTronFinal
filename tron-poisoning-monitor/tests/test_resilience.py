"""Outages and restarts: API, Telegram, database, process."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app import repository as repo
from app.domain import AlertStatus, EventType
from app.models import Transaction
from app.services.transaction_monitor import STATE_BLOCK_CURSOR
from app.services.tron_service import TronApiError, TronGridClient
from app.simulation import addresses as A
from tests.conftest import USDT, make_settings

SUCCESS = EventType.SUCCESSFUL_POISONING_EVENT.value


# 11 -----------------------------------------------------------------------
async def test_api_outage_does_not_crash_or_skip_blocks(make_app):
    h = await make_app()
    h.history(payments=8)
    await h.add()
    await h.go_live()
    cursor_before = h.app.monitor.cursor
    h.chain.outage = True
    tx = h.chain.send(A.VICTIM, A.POISON, 25_000 * USDT, ts_ms=h.now_ms)
    with pytest.raises(TronApiError):
        await h.app.monitor.step()
    assert h.app.monitor.cursor == cursor_before  # nothing skipped

    # the run loop survives the outage and catches up when the API returns
    task = asyncio.create_task(h.app.monitor.run(h.app.stop_event))
    await asyncio.sleep(0.2)
    assert not task.done()
    h.chain.outage = False
    for _ in range(100):
        await asyncio.sleep(0.05)
        if await h.events(event_type=SUCCESS):
            break
    h.app.stop_event.set()
    await task
    [ev] = await h.events(event_type=SUCCESS)
    assert ev.tx_hash == tx
    assert h.app.monitor.api_errors >= 1


async def test_trongrid_client_retries_backoff_and_errors():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        if calls["n"] == 2:
            return httpx.Response(429, headers={"Retry-After": "0.01"})
        if calls["n"] == 3:
            raise httpx.ConnectTimeout("timeout")
        return httpx.Response(200, json={"block_header": {"raw_data": {"number": 123, "timestamp": 1}}})

    s = make_settings("sqlite+aiosqlite://", tron_api_key="secret-key-123456")
    client = TronGridClient(s, transport=httpx.MockTransport(handler))
    assert await client.get_now_block_number() == 123
    assert calls["n"] == 4 and client.failures == 3
    await client.close()

    def bad(request):
        return httpx.Response(400, text="bad request")

    client = TronGridClient(s, transport=httpx.MockTransport(bad))
    with pytest.raises(TronApiError) as ei:
        await client.get_now_block_number()
    assert not ei.value.retryable
    await client.close()

    def down(request):
        return httpx.Response(502)

    client = TronGridClient(make_settings("sqlite+aiosqlite://", tron_max_retries=2), transport=httpx.MockTransport(down))
    with pytest.raises(TronApiError):
        await client.get_now_block_number()
    assert client.requests == 3
    await client.close()


async def test_api_key_header_is_sent_and_configurable():
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(200, json={"block_header": {"raw_data": {"number": 1}}})

    s = make_settings("sqlite+aiosqlite://", tron_api_key="k-abcdef", tron_api_key_header="X-API-KEY", tron_api_url="https://example.invalid/rpc")
    client = TronGridClient(s, transport=httpx.MockTransport(handler))
    await client.get_now_block_number()
    assert seen["x-api-key"] == "k-abcdef"
    await client.close()


# 12 -----------------------------------------------------------------------
async def test_telegram_outage_alert_is_retried_and_delivered_once(make_app):
    h = await make_app()
    h.history(payments=8)
    await h.add()
    await h.go_live()
    h.t.fail = True
    await h.send_live(A.VICTIM, A.POISON, 25_000 * USDT)
    await h.app.alerts.deliver_due()
    [a] = await h.alerts(alert_type="SUCCESS")
    assert a.status == AlertStatus.PENDING.value and a.attempts == 1 and a.last_error
    h.t.fail = False
    await asyncio.sleep(0.05)
    for _ in range(3):
        await h.app.alerts.deliver_due()
    [a] = await h.alerts(alert_type="SUCCESS")
    assert a.status == AlertStatus.SENT.value
    assert len(h.sent("SUCCESSFUL ADDRESS POISONING DETECTED")) == 1


# 13 -----------------------------------------------------------------------
async def test_database_outage_is_retried_without_losing_the_transfer(make_app):
    h = await make_app()
    h.history(payments=8)
    await h.add()
    await h.go_live()
    real_sf = h.app.ingestor.sf
    failures = {"n": 0}

    class FlakySessionFactory:
        def __call__(self):
            if failures["n"] < 3:
                failures["n"] += 1
                raise OperationalError("SELECT 1", {}, ConnectionError("database restarting"))
            return real_sf()

    h.app.ingestor.sf = FlakySessionFactory()
    await h.send_live(A.VICTIM, A.POISON, 25_000 * USDT)
    h.app.ingestor.sf = real_sf
    assert failures["n"] == 3
    assert len(await h.events(event_type=SUCCESS)) == 1


async def test_analysis_crash_is_recovered_from_pending_state(make_app):
    h = await make_app()
    h.history(payments=8)
    await h.add()
    await h.go_live()
    original = h.app.detector._analyze_locked

    async def boom(tx_id):
        raise RuntimeError("worker crashed mid-analysis")

    h.app.detector._analyze_locked = boom
    await h.send_live(A.VICTIM, A.POISON, 25_000 * USDT)
    assert await h.events() == []
    async with h.app.sf() as s:
        pending = (await s.execute(select(Transaction).where(Transaction.analysis_status == "PENDING"))).scalars().all()
    assert len(pending) == 1
    h.app.detector._analyze_locked = original
    assert await h.app.ingestor.recover_pending() == 1
    assert len(await h.events(event_type=SUCCESS)) == 1


# 14 -----------------------------------------------------------------------
async def test_restart_resumes_cursor_recovers_pending_and_never_duplicates(make_app):
    h1 = await make_app()
    h1.history(payments=8)
    await h1.add()
    await h1.go_live()
    # Process crashes after the transfer was stored but before it was analysed.
    tx = h1.chain.send(A.VICTIM, A.POISON, 25_000 * USDT, ts_ms=h1.now_ms)
    blk = h1.chain.blocks[h1.chain.head]
    async with h1.app.sf() as s, s.begin():
        await repo.insert_transfers(s, h1.app.ingestor.relevant(blk.transfers), source="LIVE", detected_at=h1.app.clock.now(), now=h1.app.clock.now())
    async with h1.app.sf() as s:
        cursor = int(await repo.get_state(s, STATE_BLOCK_CURSOR))
    await h1.app.close()

    h2 = await make_app(chain=h1.chain, fresh=False)
    assert await h2.app.ingestor.recover_pending() == 1
    await h2.app.monitor.step()  # resumes from the saved cursor, re-reads the block: deduplicated
    assert h2.app.monitor.cursor == h1.chain.head and cursor < h1.chain.head
    await h2.settle()
    [ev] = await h2.events(event_type=SUCCESS)
    assert ev.tx_hash == tx
    await h2.app.close()

    # Third start: nothing new, nothing re-sent.
    h3 = await make_app(chain=h1.chain, fresh=False)
    await h3.app.ingestor.recover_pending()
    await h3.app.monitor.step()
    await h3.settle()
    assert len(await h3.events(event_type=SUCCESS)) == 1
    assert len(await h3.alerts(alert_type="SUCCESS")) == 1
    assert h3.sent("SUCCESSFUL") == []


async def test_history_scan_resumes_after_interruption(make_app):
    h = await make_app(tron_page_limit=3)
    h.history(payments=10)
    await h.app.admin.add_wallet(A.VICTIM)
    calls = {"n": 0}
    original = h.chain.get_trc20_transfers

    async def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 3:
            raise TronApiError("simulated outage mid-scan")
        return await original(*a, **kw)

    h.chain.get_trc20_transfers = flaky
    await h.app.jobs.drain()  # first attempt fails on page 3
    wallet = (await h.app.admin.list_wallets())[0]
    assert wallet.history_status == "RUNNING" and wallet.history_cursor_ms
    # retry the job now
    from sqlalchemy import update

    from app.models import Job

    async with h.app.sf() as s, s.begin():
        await s.execute(update(Job).values(next_run_at=h.app.clock.now()))
    await h.app.jobs.drain()
    wallet = (await h.app.admin.list_wallets())[0]
    assert wallet.history_status == "COMPLETE"
    async with h.app.sf() as s:
        r = await repo.get_recipient(s, A.VICTIM, A.LEGIT, h.app.s.primary_token.contract)
    assert r.transaction_count == 10  # no double counting across the resumed scan


async def test_full_application_run_loop(make_app, tmp_path):
    """All workers running concurrently, as in production: add wallet -> history -> live attack -> alert -> trace."""
    from app.models import SystemLog

    h = await make_app(heartbeat_file=str(tmp_path / "hb"), confirmation_check_interval_seconds=0.05)
    h.history(payments=8)
    h.chain.mine(h.now_ms - 6000)
    runner = asyncio.create_task(h.app.run())
    await h.app.admin.add_wallet(A.VICTIM)
    for _ in range(200):
        await asyncio.sleep(0.02)
        if h.sent("HISTORY SCAN COMPLETE"):
            break
    h.chain.send(A.VICTIM, A.POISON, 25_000 * USDT, ts_ms=h.now_ms)
    for _ in range(200):
        await asyncio.sleep(0.02)
        if h.sent("FUND TRACE"):
            break
    h.app.request_stop()
    await asyncio.wait_for(runner, 10)
    assert len(h.sent("SUCCESSFUL ADDRESS POISONING DETECTED")) == 1
    assert (tmp_path / "hb").exists()
    async with h.app.sf() as s:
        logs = (await s.execute(select(SystemLog))).scalars().all()
    kinds = {r.event_type for r in logs}
    assert "SUCCESSFUL_POISONING_EVENT" in kinds and "WALLET_ADDED" in kinds
