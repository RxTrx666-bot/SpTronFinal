"""Restarts, checkpoints, API/DB failures, the per-wallet scheduler and backfill."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db import repository as repo
from app.db.models import Alert, Wallet
from app.domain import ALERT_LARGE_TRANSFER, dt_to_ms
from app.main import build_runtime
from app.monitor.scheduler import IN, STATE_BACKFILL_DONE, checkpoint_key
from app.monitor.stream import stream_key
from app.tron.client import TronApiError
from tests.conftest import make_settings
from tests.fakes import T0, WALLET_A, addr, usdt

W1 = addr("wallet-1")
X = addr("unrelated-sender")


async def poll(rt):
    while await rt.stream.poll_once():
        pass


async def large_alerts(sf):
    async with sf() as s:
        return list((await s.scalars(select(Alert).where(Alert.alert_type == ALERT_LARGE_TRANSFER))).all())


# 8
async def test_restart_resumes_from_checkpoint(settings, sf, tron):
    rt1 = await build_runtime(settings, sf, tron, now_ms=T0)
    tron.add(WALLET_A, W1, usdt("0.01"), ts=T0 + 60_000)
    tron.add(X, W1, usdt("900"), ts=T0 + 120_000)
    await poll(rt1)
    async with sf() as s:
        cp = await repo.get_checkpoint(s, stream_key(settings.usdt_contract))
    assert dt_to_ms(cp.last_timestamp) == T0 + 120_000 and cp.last_block is not None

    # "crash" + restart (much later): new runtime instance, same database
    tron.add(X, W1, usdt("1500"), ts=T0 + 600_000)  # arrived while we were down
    rt2 = await build_runtime(settings, sf, tron, now_ms=T0 + 900_000)
    assert rt2.processor.monitor_started_ms == T0  # monitoring start is persistent
    assert rt2.registry.is_monitored(rt2.registry.get(W1))  # wallets reloaded from DB
    tron.min_ts_seen.clear()
    await poll(rt2)
    # resumed at the checkpoint (minus the overlap), not at "now" and not from scratch
    assert tron.min_ts_seen[0] == T0 + 120_000 - settings.stream_overlap_seconds * 1000
    amounts = sorted(a.amount_base_units for a in await large_alerts(sf))
    assert amounts == [usdt("900"), usdt("1500")]  # downtime transfer caught, no duplicates


async def test_api_failure_does_not_advance_cursor(rt, sf, tron):
    tron.add(WALLET_A, W1, usdt("0.01"), ts=T0 + 1000)
    tron.fail_stream = 1
    with pytest.raises(TronApiError):
        await rt.stream.poll_once()
    assert rt.stream.cursor_ms == T0
    await poll(rt)
    assert rt.registry.get(W1) is not None


async def test_db_failure_does_not_advance_cursor(rt, sf, tron, monkeypatch):
    tron.add(WALLET_A, W1, usdt("0.01"), ts=T0 + 1000)

    async def boom(*a, **k):
        raise ConnectionError("db down")

    monkeypatch.setattr(rt.processor, "process", boom)
    with pytest.raises(ConnectionError):
        await rt.stream.poll_once()
    assert rt.stream.cursor_ms == T0
    monkeypatch.undo()
    await poll(rt)
    assert rt.registry.get(W1) is not None


async def test_failed_batch_leaves_no_partial_state(rt, sf, tron, monkeypatch):
    """Transfer, wallet and alert commit atomically: a crash mid-batch rolls everything back."""
    tron.add(WALLET_A, W1, usdt("0.01"), ts=T0 + 1000)
    tron.add(X, W1, usdt("700"), ts=T0 + 2000)
    real = repo.insert_alert

    async def crash(*a, **k):
        raise ConnectionError("lost connection")

    monkeypatch.setattr(repo, "insert_alert", crash)
    with pytest.raises(ConnectionError):
        await rt.stream.poll_once()
    async with sf() as s:
        assert (await s.scalars(select(Wallet).where(Wallet.address == W1))).first() is None
    assert rt.registry.get(W1) is None  # memory not polluted by the rolled-back batch
    monkeypatch.setattr(repo, "insert_alert", real)
    await poll(rt)
    assert len(await large_alerts(sf)) == 1


async def test_late_out_of_order_event_recovered_by_scheduler(rt, sf, tron):
    """Stream missed a large transfer (gap / late event): the per-wallet scheduler catches it."""
    tron.add(WALLET_A, W1, usdt("0.01"), ts=T0 + 1000)
    big = tron.add(X, W1, usdt("2500"), ts=T0 + 5000)
    tron.hidden_from_stream.add(big["transaction_id"])
    await poll(rt)
    assert await large_alerts(sf) == []
    n = await rt.scheduler.sweep_once_inline()
    assert n == 2  # root (out) + W1 (in)
    [a] = await large_alerts(sf)
    assert a.tx_hash == big["transaction_id"] and a.amount_base_units == usdt("2500")
    assert rt.scheduler.recovered == 1
    # second sweep: nothing new, no duplicate, checkpoint advanced
    await rt.scheduler.sweep_once_inline()
    assert len(await large_alerts(sf)) == 1
    async with sf() as s:
        cp = await repo.get_checkpoint(s, checkpoint_key(W1, IN))
    assert dt_to_ms(cp.last_timestamp) == T0 + 5000


async def test_scheduler_recovers_missed_discovery(rt, sf, tron):
    disc = tron.add(WALLET_A, W1, usdt("0.01"), ts=T0 + 1000)
    tron.hidden_from_stream.add(disc["transaction_id"])
    await poll(rt)
    assert rt.registry.get(W1) is None
    await rt.scheduler.sweep_once_inline()
    assert rt.registry.is_monitored(rt.registry.get(W1))


async def test_scheduler_only_resolves_large_candidates(rt, sf, tron):
    tron.add(WALLET_A, W1, usdt("0.01"), ts=T0 + 1000)
    await poll(rt)
    for i in range(30):
        tron.add(X, W1, usdt("10"), ts=T0 + 2000 + i)  # small incoming: never resolved
    before = tron.calls["tx_events"]
    await rt.scheduler.sweep_once_inline()
    assert tron.calls["tx_events"] == before


async def test_backfill_discovers_history_without_alerts(sf, tron):
    settings = make_settings(backfill_enabled=True, backfill_lookback_days=30)
    old1, old2 = addr("old-1"), addr("old-2")
    day = 86_400_000
    tron.add(WALLET_A, old1, usdt("0.01"), ts=T0 - 20 * day)
    tron.add(X, old1, usdt("5000"), ts=T0 - 19 * day)  # historical large: must not alert
    tron.add(WALLET_A, old2, 1, ts=T0 - 2 * day)
    tron.add(WALLET_A, addr("too-old"), usdt("1"), ts=T0 - 40 * day)  # outside lookback
    rt = await build_runtime(settings, sf, tron, now_ms=T0)
    import asyncio

    await rt.scheduler.run_backfill(asyncio.Event())
    assert {w.address for w in rt.registry.monitored()} == {old1, old2}
    assert await large_alerts(sf) == []
    async with sf() as s:
        assert await repo.get_state(s, STATE_BACKFILL_DONE) == "1"
        cp = await repo.get_checkpoint(s, checkpoint_key(old1, IN))
    assert dt_to_ms(cp.last_timestamp) == T0  # monitored from monitoring start onwards

    # future incoming transfer to a backfilled wallet alerts
    tron.add(X, old1, usdt("650"), ts=T0 + 5000)
    await poll(rt)
    [a] = await large_alerts(sf)
    assert a.discovered_wallet == old1


async def test_backfill_historical_alerts_when_configured(sf, tron):
    settings = make_settings(backfill_enabled=True, alert_on_historical=True)
    tron.add(WALLET_A, W1, usdt("700"), ts=T0 - 86_400_000)
    rt = await build_runtime(settings, sf, tron, now_ms=T0)
    import asyncio

    await rt.scheduler.run_backfill(asyncio.Event())
    assert len(await large_alerts(sf)) == 1


async def test_backfill_is_resumable(sf, tron):
    import asyncio

    settings = make_settings(backfill_enabled=True, backfill_lookback_days=3)
    rt = await build_runtime(settings, sf, tron, now_ms=T0)
    async with sf() as s, s.begin():
        await repo.set_state(s, "backfill_next_ms", str(T0 - 86_400_000))  # 2 of 3 days already done
    tron.add(WALLET_A, addr("skipped"), 1, ts=T0 - 2 * 86_400_000)
    tron.add(WALLET_A, W1, 1, ts=T0 - 3600_000)
    await rt.scheduler.run_backfill(asyncio.Event())
    assert {w.address for w in rt.registry.monitored()} == {W1}


async def test_worker_pool_sweeps_many_wallets(sf, tron):
    """The real scheduler loop: fixed worker pool, every wallet checked, alerts recovered."""
    import asyncio

    settings = make_settings(reconcile_workers=3, reconcile_interval_seconds=3600)
    rt = await build_runtime(settings, sf, tron, now_ms=T0)
    ws = [addr(f"pool-{i}") for i in range(60)]
    for i, w in enumerate(ws):
        tron.add(WALLET_A, w, 1, ts=T0 + 1000 + i)
    await poll(rt)
    hidden = tron.add(X, ws[42], usdt("777"), ts=T0 + 9000)
    tron.hidden_from_stream.add(hidden["transaction_id"])
    before = tron.calls["account_trc20"]
    stop = asyncio.Event()
    task = asyncio.create_task(rt.scheduler.run(stop))
    for _ in range(200):
        if rt.scheduler.sweeps:
            break
        await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(task, 5)
    assert rt.scheduler.sweeps >= 1
    assert tron.calls["account_trc20"] - before >= 61  # 60 wallets (in) + Wallet A (out)
    [a] = await large_alerts(sf)
    assert a.discovered_wallet == ws[42]
