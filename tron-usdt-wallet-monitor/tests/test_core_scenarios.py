"""The required scenarios (spec section 23) plus edge cases, end-to-end through
the live stream -> normalizer -> processor -> PostgreSQL."""

from __future__ import annotations

from sqlalchemy import func, select

from app.db.models import Alert, TransferRow, Wallet
from app.domain import ALERT_DISCOVERY, ALERT_LARGE_TRANSFER, ALERT_PENDING, ALERT_SUPPRESSED, WALLET_DISCOVERED
from app.main import build_runtime
from app.tron.normalizer import parse_events
from tests.conftest import make_settings
from tests.fakes import OTHER_TOKEN, T0, WALLET_A, addr, make_event, usdt

W1 = addr("wallet-1")
X = addr("unrelated-sender")


async def wallets(sf) -> list[Wallet]:
    async with sf() as s:
        return list((await s.scalars(select(Wallet).where(Wallet.wallet_type == WALLET_DISCOVERED))).all())


async def alerts(sf, alert_type=ALERT_LARGE_TRANSFER) -> list[Alert]:
    async with sf() as s:
        return list((await s.scalars(select(Alert).where(Alert.alert_type == alert_type).order_by(Alert.id))).all())


async def poll(rt):
    while await rt.stream.poll_once():
        pass


# 1
async def test_wallet_a_sends_0_01_discovers_wallet(rt, sf, tron):
    ev = tron.add(WALLET_A, W1, usdt("0.01"), ts=T0 + 1000)
    await poll(rt)
    ws = await wallets(sf)
    assert [w.address for w in ws] == [W1]
    assert ws[0].root_wallet == WALLET_A and ws[0].hop == 1 and ws[0].active
    assert ws[0].first_seen_tx == ev["transaction_id"]
    assert ws[0].first_seen_amount_base_units == 10_000
    assert rt.registry.is_monitored(rt.registry.get(W1))


# 2
async def test_dust_0_000001_discovers_wallet(rt, sf, tron):
    tron.add(WALLET_A, W1, 1, ts=T0 + 1000)  # 0.000001 USDT = 1 base unit
    await poll(rt)
    assert [w.address for w in await wallets(sf)] == [W1]


# 3, 4, 5 and the 499.999999 boundary
async def test_threshold_is_inclusive_500(rt, sf, tron):
    tron.add(WALLET_A, W1, usdt("0.01"), ts=T0 + 1000)
    tron.add(X, W1, usdt("499"), ts=T0 + 2000)
    tron.add(X, W1, usdt("499.999999"), ts=T0 + 3000)
    exact = tron.add(X, W1, usdt("500"), ts=T0 + 4000)
    above = tron.add(X, W1, usdt("500.000001"), ts=T0 + 5000)
    await poll(rt)
    got = await alerts(sf)
    assert [a.tx_hash for a in got] == [exact["transaction_id"], above["transaction_id"]]
    assert [a.amount_base_units for a in got] == [500_000_000, 500_000_001]
    assert all(a.status == ALERT_PENDING for a in got)


# 6
async def test_large_transfer_from_unrelated_wallet_alerts(rt, sf, tron):
    disc = tron.add(WALLET_A, W1, usdt("0.01"), ts=T0 + 1000)
    big = tron.add(X, W1, usdt("10000"), ts=T0 + 3 * 3600_000 + 17 * 60_000)
    await poll(rt)
    [a] = await alerts(sf)
    assert a.sender == X and a.discovered_wallet == W1 and a.root_wallet == WALLET_A
    assert a.tx_hash == big["transaction_id"]
    assert a.discovery_tx == disc["transaction_id"]
    assert a.amount_base_units == usdt("10000")
    assert (a.transfer_timestamp - a.discovered_at).total_seconds() == 3 * 3600 + 17 * 60 - 1


# 7
async def test_same_transaction_processed_twice_one_alert(rt, sf, tron):
    tron.add(WALLET_A, W1, usdt("0.01"), ts=T0 + 1000)
    big = tron.add(X, W1, usdt("750"), ts=T0 + 2000)
    await poll(rt)
    await poll(rt)  # the overlap window re-reads the same events
    transfers, _ = parse_events([big], rt.settings.usdt_contract)
    res = await rt.processor.process(transfers + transfers, source="reconcile")  # and again, directly
    assert res.stored == 0 and res.duplicates == 1
    assert len(await alerts(sf)) == 1
    async with sf() as s:
        assert await s.scalar(select(func.count()).select_from(TransferRow)) == 2


# 9
async def test_non_usdt_trc20_ignored(rt, sf, tron, settings):
    tron.add(WALLET_A, W1, usdt("0.01"), ts=T0 + 1000, contract=OTHER_TOKEN)
    await poll(rt)
    assert await wallets(sf) == []
    # and an other-token event never parses as USDT
    transfers, rejected = parse_events([make_event(X, W1, usdt("9999"), ts=T0, contract=OTHER_TOKEN)], settings.usdt_contract)
    assert transfers == [] and rejected == {"not_usdt": 1}


# 10
async def test_trx_transfer_ignored(rt, sf, tron, settings):
    trx_tx = {  # a native TRX TransferContract as returned by /v1/accounts/{a}/transactions
        "txID": "ab" * 32,
        "raw_data": {"contract": [{"type": "TransferContract", "parameter": {"value": {"amount": 1_000_000_000}}}]},
        "block_timestamp": T0,
    }
    transfers, rejected = parse_events([trx_tx], settings.usdt_contract)
    assert transfers == [] and rejected == {"not_a_contract_event": 1}
    res = await rt.processor.process(transfers, source="stream")
    assert res.stored == 0 and await wallets(sf) == []


# 11
async def test_already_discovered_wallet_not_duplicated(rt, sf, tron):
    first = tron.add(WALLET_A, W1, usdt("0.01"), ts=T0 + 1000)
    tron.add(WALLET_A, W1, usdt("0.02"), ts=T0 + 2000)
    await poll(rt)
    tron.add(WALLET_A, W1, usdt("0.05"), ts=T0 + 3000)
    await poll(rt)
    ws = await wallets(sf)
    assert len(ws) == 1 and ws[0].first_seen_tx == first["transaction_id"]


# ------------------------------------------------------------ edge cases


async def test_discovery_and_large_in_same_batch(rt, sf, tron):
    """Wallet discovered and funded within one poll page."""
    tron.add(WALLET_A, W1, usdt("0.01"), ts=T0 + 1000)
    tron.add(X, W1, usdt("2000"), ts=T0 + 1000, idx=1)
    await poll(rt)
    assert len(await alerts(sf)) == 1


async def test_large_transfer_before_discovery_does_not_alert(rt, sf, tron):
    tron.add(X, W1, usdt("5000"), ts=T0 + 1000)  # W1 not discovered yet
    tron.add(WALLET_A, W1, usdt("0.01"), ts=T0 + 2000)
    await poll(rt)
    assert await alerts(sf) == []


async def test_outgoing_from_discovered_wallet_ignored(rt, sf, tron):
    tron.add(WALLET_A, W1, usdt("0.01"), ts=T0 + 1000)
    tron.add(W1, X, usdt("5000"), ts=T0 + 2000)
    await poll(rt)
    assert await alerts(sf) == []
    assert [w.address for w in await wallets(sf)] == [W1]  # MAX_HOPS=1: X is not discovered


async def test_self_transfer_does_not_alert_by_default(rt, sf, tron):
    tron.add(WALLET_A, W1, usdt("0.01"), ts=T0 + 1000)
    tron.add(W1, W1, usdt("5000"), ts=T0 + 2000)
    tron.add(WALLET_A, WALLET_A, usdt("1"), ts=T0 + 3000)  # root self-transfer is not a discovery
    await poll(rt)
    assert await alerts(sf) == []
    assert [w.address for w in await wallets(sf)] == [W1]


async def test_wallet_a_large_send_to_new_wallet_alerts(rt, sf, tron):
    """The discovering transfer itself is a large incoming transfer."""
    tron.add(WALLET_A, W1, usdt("600"), ts=T0 + 1000)
    await poll(rt)
    [a] = await alerts(sf)
    assert a.sender == WALLET_A and a.discovered_wallet == W1


async def test_transfers_before_monitoring_start_never_alert(rt, sf, tron):
    tron.add(WALLET_A, W1, usdt("0.01"), ts=T0 - 20_000)
    tron.add(X, W1, usdt("9000"), ts=T0 - 10_000)  # inside the overlap window, before start
    await poll(rt)
    assert [w.address for w in await wallets(sf)] == [W1]  # still discovered
    assert await alerts(sf) == []


async def test_paused_records_but_suppresses(rt, sf, tron):
    await rt.set_paused(True)
    tron.add(WALLET_A, W1, usdt("0.01"), ts=T0 + 1000)
    tron.add(X, W1, usdt("800"), ts=T0 + 2000)
    await poll(rt)
    [a] = await alerts(sf)
    assert a.status == ALERT_SUPPRESSED


async def test_alert_on_discovery_option(sf, tron):
    rt = await build_runtime(make_settings(alert_on_discovery=True), sf, tron, now_ms=T0)
    tron.add(WALLET_A, W1, usdt("0.01"), ts=T0 + 1000)
    await poll(rt)
    [d] = await alerts(sf, ALERT_DISCOVERY)
    assert d.discovered_wallet == W1 and d.amount_base_units == 10_000
    assert await alerts(sf) == []


async def test_max_hops_2_expands_recursively(sf, tron):
    rt = await build_runtime(make_settings(max_hops=2), sf, tron, now_ms=T0)
    W2 = addr("wallet-2")
    tron.add(WALLET_A, W1, usdt("0.01"), ts=T0 + 1000)
    tron.add(W1, W2, usdt("0.01"), ts=T0 + 2000)
    tron.add(X, W2, usdt("700"), ts=T0 + 3000)
    await poll(rt)
    ws = {w.address: w.hop for w in await wallets(sf)}
    assert ws == {W1: 1, W2: 2}
    [a] = await alerts(sf)
    assert a.discovered_wallet == W2


async def test_custom_threshold(sf, tron):
    rt = await build_runtime(make_settings(alert_min_amount_usdt="1000.5"), sf, tron, now_ms=T0)
    assert rt.settings.alert_min_base_units == 1_000_500_000
    tron.add(WALLET_A, W1, usdt("0.01"), ts=T0 + 1000)
    tron.add(X, W1, usdt("1000.499999"), ts=T0 + 2000)
    tron.add(X, W1, usdt("1000.5"), ts=T0 + 3000)
    await poll(rt)
    assert [a.amount_base_units for a in await alerts(sf)] == [1_000_500_000]


async def test_thousands_of_wallets_single_request_per_poll(rt, sf, tron):
    """2,000 discovered wallets do not multiply API calls: one stream request per poll."""
    ws = [addr(f"bulk-{i}") for i in range(2000)]
    for i, w in enumerate(ws):
        tron.add(WALLET_A, w, 1, ts=T0 + 1000 + i)
    await poll(rt)
    assert len(rt.registry.monitored()) == 2000
    before = tron.calls["contract_events"]
    tron.add(X, ws[1234], usdt("501"), ts=T0 + 10_000)
    tron.events = [e for e in tron.events if e["block_timestamp"] >= T0 + 9_000]  # API returns only recent events
    await poll(rt)
    assert tron.calls["contract_events"] - before == 1
    [a] = await alerts(sf)
    assert a.discovered_wallet == ws[1234]
