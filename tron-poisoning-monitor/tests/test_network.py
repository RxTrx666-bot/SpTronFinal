"""Network-wide detection: poisoning on wallets nobody added with /add."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select, update

from app import repository as repo
from app.domain import EventType
from app.models import HistoricalRecipient, Transaction
from app.simulation import addresses as A
from app.utils.address import address_from_seed
from tests.conftest import USDT

SUCCESS = EventType.SUCCESSFUL_POISONING_EVENT.value
USDT_C = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


async def net_app(make_app, **kw):
    h = await make_app(network_wide=True, **kw)
    await h.go_live()
    return h


async def pair(h, sender, rcpt) -> HistoricalRecipient | None:
    async with h.app.sf() as s:
        return await repo.get_recipient(s, sender, rcpt, USDT_C)


async def test_detects_poisoning_on_any_wallet_without_add(make_app):
    h = await net_app(make_app)
    assert await h.app.admin.list_wallets() == []  # nothing added
    for amt in (20_000, 21_000, 19_500):
        await h.send_live(A.VICTIM, A.LEGIT, amt * USDT)
    r = await pair(h, A.VICTIM, A.LEGIT)
    assert r.transaction_count == 3 and r.total_amount == 60_500 * USDT
    tx = await h.send_live(A.VICTIM, A.POISON, 25_000 * USDT)
    await h.app.alerts.deliver_due()
    [ev] = await h.events(event_type=SUCCESS)
    assert ev.tx_hash == tx and ev.victim_wallet == A.VICTIM and ev.legitimate_recipient == A.LEGIT
    assert ev.legit_tx_count == 3
    assert len(h.sent("SUCCESSFUL ADDRESS POISONING DETECTED")) == 1
    await h.settle()  # investigation + trace jobs run for network events too


async def test_network_dust_is_recorded_as_evidence(make_app):
    h = await net_app(make_app)
    await h.send_live(A.VICTIM, A.LEGIT, 30_000 * USDT)
    dust = await h.send_live(A.POISON, A.VICTIM, 1)  # attacker reacts with 0.000001 USDT from the look-alike
    await h.send_live(A.POISON, address_from_seed("random-dust-target"), 1)  # unrelated dust: not stored
    await h.send_live(A.VICTIM, A.POISON, 25_000 * USDT)
    [ev] = await h.events(event_type=SUCCESS)
    assert ev.poisoning_tx_observed == "YES"
    assert any(e.tx_hash == dust for e in await h.evidence(ev.id))
    async with h.app.sf() as s:
        stored = (await s.execute(select(Transaction).where(Transaction.source == "NETWORK_DUST"))).scalars().all()
    assert [t.tx_hash for t in stored] == [dust]


async def test_ordinary_traffic_creates_no_incidents_and_no_transaction_rows(make_app):
    h = await net_app(make_app)
    for i in range(5):
        await h.send_live(address_from_seed(f"s{i}"), address_from_seed(f"r{i}"), 1_000 * USDT)
    await h.send_live(A.VICTIM, A.LEGIT, 30_000 * USDT)
    await h.send_live(A.VICTIM, A.PREFIX_ONLY, 30_000 * USDT)  # shares only the prefix
    await h.send_live(A.VICTIM, A.SUFFIX_ONLY, 30_000 * USDT)  # shares only the suffix
    await h.settle()
    assert await h.events() == []
    async with h.app.sf() as s:
        assert (await s.execute(select(Transaction))).first() is None  # only look-alike activity is stored in full
    assert h.app.network.stats["pairs_upserted"] == 8


async def test_small_network_payment_is_recorded_but_not_alerted(make_app):
    h = await net_app(make_app, network_min_alert_usdt="100")
    await h.send_live(A.VICTIM, A.LEGIT, 30_000 * USDT)
    await h.send_live(A.VICTIM, A.LEGIT, 30_000 * USDT)
    await h.send_live(A.VICTIM, A.POISON, 50 * USDT)
    await h.settle()
    assert len(await h.events()) == 1
    assert h.sent("SUCCESSFUL") == []


async def test_restart_does_not_double_count_network_memory(make_app):
    h1 = await net_app(make_app)
    await h1.send_live(A.VICTIM, A.LEGIT, 1_000 * USDT)
    await h1.app.close()
    h2 = await make_app(chain=h1.chain, fresh=False, network_wide=True)
    await h2.app.monitor.step()  # resumes after the saved cursor: the block is not re-counted
    assert (await pair(h2, A.VICTIM, A.LEGIT)).transaction_count == 1


async def test_watched_wallets_and_network_mode_together(make_app):
    h = await make_app(network_wide=True)
    h.history(payments=6)
    await h.add(A.VICTIM)
    await h.go_live()
    await h.send_live(A.VICTIM, A.POISON, 25_000 * USDT)
    await h.send_live(A.VICTIM_2, A.LEGIT_B, 9_000 * USDT)
    await h.send_live(A.VICTIM_2, A.LEGIT_B, 9_000 * USDT)
    await h.send_live(A.VICTIM_2, A.POISON_B, 9_000 * USDT)
    await h.app.alerts.deliver_due()
    victims = {e.victim_wallet for e in await h.events(event_type=SUCCESS)}
    assert victims == {A.VICTIM, A.VICTIM_2}
    assert len(h.sent("SUCCESSFUL ADDRESS POISONING DETECTED")) == 2


async def test_prune_forgets_old_network_memory_but_keeps_watched_and_incidents(make_app):
    h = await net_app(make_app, network_memory_days=7)
    await h.send_live(A.VICTIM, A.LEGIT, 30_000 * USDT)
    await h.send_live(A.VICTIM, A.POISON, 25_000 * USDT)  # incident
    await h.send_live(address_from_seed("old-sender"), address_from_seed("old-rcpt"), 5 * USDT)
    old = h.app.clock.now() - timedelta(days=30)
    async with h.app.sf() as s, s.begin():
        await s.execute(update(HistoricalRecipient).values(last_seen=old))
        await s.execute(update(Transaction).values(block_timestamp=old))
    await h.app.network.prune()
    async with h.app.sf() as s:
        left = {(r.victim_wallet, r.recipient_wallet) for r in (await s.execute(select(HistoricalRecipient))).scalars()}
        txs = (await s.execute(select(Transaction))).scalars().all()
    assert left == {(A.VICTIM, A.POISON)}  # flagged look-alike kept; ordinary old pairs pruned
    assert len(txs) == 1 and len(await h.events()) == 1  # incident and its transaction kept
