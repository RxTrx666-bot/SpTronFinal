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


# ----------------------------------------------------------------- real-world attack pattern
async def _victim_with_old_history(make_app):
    """Victim paid LEGIT several times BEFORE the bot started; the bot's memory knows nothing about it."""
    from app.simulation.chain import SimulatedChain

    chain = SimulatedChain()
    t0 = int(__import__("time").time() * 1000) - 30 * 86_400_000
    chain.head_ts = t0 - 3000
    for i in range(4):
        chain.send(A.VICTIM, A.LEGIT, (20_000 + i) * USDT, ts_ms=t0 + i * 86_400_000)
    for _ in range(25):
        chain.mine()
    h = await make_app(chain=chain, network_wide=True)
    await h.go_live()
    return h


async def test_fake_token_poisoning_detected_without_prior_memory(make_app):
    h = await _victim_with_old_history(make_app)
    fake_usdt = address_from_seed("fake-usdt-contract")
    await h.send_live(A.POISON, A.VICTIM, 20_000 * USDT, token=fake_usdt)  # fake "USDT" shows the look-alike in history
    tx = await h.send_live(A.VICTIM, A.POISON, 25_000 * USDT)
    await h.app.alerts.deliver_due()
    [ev] = await h.events(event_type=SUCCESS)
    assert ev.tx_hash == tx and ev.legitimate_recipient == A.LEGIT and ev.legit_tx_count == 4
    assert ev.poisoning_tx_observed == "YES"
    assert any("fake 'USDT'" in e.description for e in await h.evidence(ev.id))
    assert len(h.sent("SUCCESSFUL ADDRESS POISONING DETECTED")) == 1
    assert h.app.network.stats["history_lookups"] == 1


async def test_tiny_trx_poisoning_with_short_prefix_pattern(make_app):
    h = await _victim_with_old_history(make_app)
    h.chain.trx(A.POISON_SHORT, A.VICTIM, 1)  # 0.000001 TRX from a TLeg…Wr2c look-alike
    h.chain.mine(h.chain.head_ts + 3000)
    await h.app.monitor.step()
    await h.send_live(A.VICTIM, A.POISON_SHORT, 25_000 * USDT)
    [ev] = await h.events(event_type=SUCCESS)
    assert ev.suspicious_recipient == A.POISON_SHORT and ev.legitimate_recipient == A.LEGIT


async def test_zero_value_transferfrom_poisoning_without_prior_memory(make_app):
    h = await _victim_with_old_history(make_app)
    await h.send_live(A.VICTIM, A.POISON, 0, initiator=address_from_seed("attacker"))  # transferFrom(victim, fake, 0)
    await h.send_live(A.VICTIM, A.POISON, 25_000 * USDT)
    [ev] = await h.events(event_type=SUCCESS)
    assert ev.poisoning_tx_observed == "YES"


async def test_paying_someone_who_sent_a_test_transfer_is_not_flagged(make_app):
    h = await _victim_with_old_history(make_app)
    friend = address_from_seed("friend")
    await h.send_live(friend, A.VICTIM, 1 * USDT)  # ordinary small test payment
    await h.send_live(A.VICTIM, friend, 5_000 * USDT)
    await h.settle()
    assert await h.events() == []
    assert h.app.network.stats["contact_hits"] == 1  # checked against the real history, no look-alike


# ----------------------------------------------------------------- replay of the real missed attack (2026-10-02)
async def _replay_real_attack(h, start_ms):
    """05:40:45 victim pays REAL (test payment) · 05:41:21 fake dusts victim · 05:42:03 victim pays FAKE 19,900 USDT."""
    h.chain.send(A.VICTIM, A.LEGIT, 10 * USDT, ts_ms=start_ms)
    h.chain.send(A.POISON_SHORT, A.VICTIM, 1, ts_ms=start_ms + 36_000)
    tx = h.chain.send(A.VICTIM, A.POISON_SHORT, 19_900 * USDT, ts_ms=start_ms + 78_000)
    await h.app.monitor.step()
    await h.app.alerts.deliver_due()
    return tx


async def test_real_attack_replay_test_payment_dust_main_payment(make_app):
    h = await net_app(make_app)
    tx = await _replay_real_attack(h, h.chain.head_ts + 3000)
    [ev] = await h.events(event_type=SUCCESS)
    assert ev.tx_hash == tx and ev.legitimate_recipient == A.LEGIT
    keys = {s["key"] for s in ev.score_breakdown}
    assert "rapid_poisoning_sequence" in keys and "legit_weak_relationship" not in keys
    assert len(h.sent("SUCCESSFUL ADDRESS POISONING DETECTED")) == 1


async def test_real_attack_replay_when_real_payment_predates_the_bot(make_app):
    from app.simulation.chain import SimulatedChain

    chain = SimulatedChain()
    now = int(__import__("time").time() * 1000)
    chain.head_ts = now - 600_000
    chain.send(A.VICTIM, A.LEGIT, 10 * USDT, ts_ms=now - 300_000)  # before the bot starts
    for _ in range(25):
        chain.mine()
    h = await make_app(chain=chain, network_wide=True)
    await h.go_live()
    h.chain.send(A.POISON_SHORT, A.VICTIM, 1, ts_ms=h.chain.head_ts + 3000)
    await h.app.monitor.step()
    await h.send_live(A.VICTIM, A.POISON_SHORT, 19_900 * USDT)
    await h.app.alerts.deliver_due()
    [ev] = await h.events(event_type=SUCCESS)
    assert ev.legitimate_recipient == A.LEGIT
    assert len(h.sent("SUCCESSFUL ADDRESS POISONING DETECTED")) == 1


async def test_below_threshold_lookalike_payment_is_still_sent_as_possible(make_app):
    h = await net_app(make_app, confidence_threshold=99)
    await h.send_live(A.VICTIM, A.LEGIT, 30_000 * USDT)
    await h.send_live(A.VICTIM, A.POISON, 25_000 * USDT)
    await h.app.alerts.deliver_due()
    assert h.sent("POSSIBLE ADDRESS POISONING")


async def test_diagnose_rescore_upgrades_old_candidate(make_app, capsys):
    from app.diagnose import rescore

    # recorded under the OLD rules (no timing signal) -> only a candidate, like the real case
    h = await net_app(make_app, risk_weights='{"rapid_poisoning_sequence": 0}', notify_candidates=False)
    await _replay_real_attack(h, h.chain.head_ts + 3000)
    [ev] = await h.events()
    assert ev.event_type == "POISONING_CANDIDATE"
    from app.config import Settings

    s = Settings(_env_file=None, **{**h.app.s.model_dump(), "risk_weights": ""})
    await rescore(s, h.app.sf, [ev], apply=True)
    assert "scores" in capsys.readouterr().out
    [ev] = await h.events()
    assert ev.event_type == SUCCESS
    await h.app.alerts.deliver_due()
    assert h.sent("SUCCESSFUL ADDRESS POISONING DETECTED")


async def test_lookalike_planting_with_more_than_dust_amount(make_app):
    """Variant seen in the real case: the look-alike sends a 'normal looking' small amount, not dust."""
    h = await net_app(make_app)
    start = h.chain.head_ts + 3000
    h.chain.send(A.VICTIM, A.LEGIT, 10 * USDT, ts_ms=start)
    plant = h.chain.send(A.POISON_SHORT, A.VICTIM, 5 * USDT, ts_ms=start + 36_000)  # 5 USDT > DUST_MAX (1 USDT)
    h.chain.send(A.VICTIM, A.POISON_SHORT, 19_900 * USDT, ts_ms=start + 78_000)
    await h.app.monitor.step()
    await h.app.alerts.deliver_due()
    [ev] = await h.events(event_type=SUCCESS)
    assert ev.poisoning_tx_observed == "YES"
    assert "rapid_poisoning_sequence" in {s["key"] for s in ev.score_breakdown}
    assert any(e.tx_hash == plant for e in await h.evidence(ev.id))
    assert len(h.sent("SUCCESSFUL ADDRESS POISONING DETECTED")) == 1


async def test_diagnose_dry_run_and_apply_with_planting_transfer(make_app, capsys):
    from app.config import Settings
    from app.diagnose import rescore
    from app.domain import TokenTransfer

    h = await net_app(make_app, risk_weights='{"rapid_poisoning_sequence": 0}', dust_max_amount_usdt="0", notify_candidates=False)
    start = h.chain.head_ts + 3000
    h.chain.send(A.VICTIM, A.LEGIT, 10 * USDT, ts_ms=start)
    h.chain.send(A.POISON_SHORT, A.VICTIM, 5 * USDT, ts_ms=start + 36_000)
    h.chain.send(A.VICTIM, A.POISON_SHORT, 19_900 * USDT, ts_ms=start + 78_000)
    await h.app.monitor.step()
    [ev] = await h.events()
    assert ev.event_type == "POISONING_CANDIDATE"
    s = Settings(_env_file=None, **{**h.app.s.model_dump(), "risk_weights": "", "dust_max_amount_usdt": "1"})
    plant = TokenTransfer("ab" * 32, USDT_C, A.POISON_SHORT, A.VICTIM, 5 * USDT, start + 36_000)
    await rescore(s, h.app.sf, [ev], apply=False, planted={(A.VICTIM, A.POISON_SHORT): [plant]})
    assert "SUCCESSFUL_POISONING_EVENT" in capsys.readouterr().out
    assert (await h.events())[0].event_type == "POISONING_CANDIDATE"  # dry run changed nothing
    await rescore(s, h.app.sf, [ev], apply=True, planted={(A.VICTIM, A.POISON_SHORT): [plant]})
    assert (await h.events())[0].event_type == SUCCESS
    await h.app.alerts.deliver_due()
    assert h.sent("SUCCESSFUL ADDRESS POISONING DETECTED")


async def test_trace_ignores_attacker_hub_side_transfers(make_app):
    """Real trace: fake -> hub (19,900); hub only sends 10 USDT to new look-alikes -> funds still at the hub."""
    h = await net_app(make_app)
    await _replay_real_attack(h, h.chain.head_ts + 3000)
    hub = address_from_seed("attacker-hub")
    t = h.chain.head_ts
    h.chain.send(A.POISON_SHORT, hub, 19_900 * USDT, ts_ms=t + 12_000)
    for i in range(3):
        new_fake = address_from_seed(f"new-fake-{i}")
        other_victim = address_from_seed(f"other-victim-{i}")
        h.chain.send(hub, new_fake, 10 * USDT, ts_ms=t + 600_000 + i * 60_000)
        h.chain.send(new_fake, other_victim, 10 * USDT, ts_ms=t + 603_000 + i * 60_000)
        h.chain.send(other_victim, address_from_seed(f"their-real-{i}"), 60_000 * USDT, ts_ms=t + 900_000 + i * 60_000)
    await h.settle()
    [ev] = await h.events(event_type=SUCCESS)
    from app.services import report_service as rs

    async with h.app.sf() as s:
        b = await rs.load_bundle(s, ev.id)
    hops = b.trace_hops()
    assert [(x.hop, x.to_address) for x in hops] == [(1, hub)]
    assert "funds not moved on yet" in hops[0].terminal_reason and "3 smaller transfer(s) ignored" in hops[0].terminal_reason
