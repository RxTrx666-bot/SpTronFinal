"""End-to-end detection scenarios on the simulated chain (no real blockchain)."""

from __future__ import annotations

import re

from sqlalchemy import select

from app.domain import EventType
from app.models import AddressSimilarityMatch, HistoricalRecipient, Transaction
from app.simulation import addresses as A
from app.utils.address import address_from_seed
from tests.conftest import USDT

SUCCESS = EventType.SUCCESSFUL_POISONING_EVENT.value
CANDIDATE = EventType.POISONING_CANDIDATE.value
ATTEMPT = EventType.POISONING_ATTEMPT.value


async def recipient(h, victim, rcpt) -> HistoricalRecipient | None:
    async with h.app.sf() as s:
        return (
            await s.execute(select(HistoricalRecipient).where(HistoricalRecipient.victim_wallet == victim, HistoricalRecipient.recipient_wallet == rcpt))
        ).scalar_one_or_none()


# 1 -------------------------------------------------------------------------
async def test_normal_repeated_recipient_updates_stats_without_alert(make_app):
    h = await make_app()
    h.history(payments=5, amount=1_000 * USDT)
    await h.add()
    r = await recipient(h, A.VICTIM, A.LEGIT)
    assert r.transaction_count == 5
    assert r.total_amount == sum(1_000 * USDT + i * USDT for i in range(5))
    await h.go_live()
    await h.send_live(A.VICTIM, A.LEGIT, 1_500 * USDT)
    await h.settle()
    r = await recipient(h, A.VICTIM, A.LEGIT)
    assert r.transaction_count == 6
    assert r.largest_amount == 1_500 * USDT
    assert r.smallest_amount == 1_000 * USDT
    assert r.average_amount == r.total_amount // 6
    assert r.first_seen < r.last_seen
    assert await h.events() == []
    assert h.sent("SUCCESSFUL") == []


# 2 -------------------------------------------------------------------------
async def test_new_unrelated_recipient_is_not_alerted(make_app):
    h = await make_app()
    h.history()
    await h.add()
    await h.go_live()
    other = address_from_seed("unrelated-new-recipient")
    await h.send_live(A.VICTIM, other, 50_000 * USDT)
    await h.settle()
    assert await h.events() == []
    assert (await recipient(h, A.VICTIM, other)).transaction_count == 1


# 3 -------------------------------------------------------------------------
async def test_similar_recipient_that_victim_already_uses_is_not_alerted(make_app):
    h = await make_app()
    # Victim legitimately uses BOTH similar addresses (e.g. two deposit addresses that happen to look alike).
    h.history(payments=5, extra=[(A.POISON, 5_000 * USDT)] * 4)
    await h.add()
    await h.go_live()
    await h.send_live(A.VICTIM, A.POISON, 20_000 * USDT)
    await h.settle()
    assert [e for e in await h.events() if e.event_type != ATTEMPT] == []
    assert h.sent("SUCCESSFUL") == []


# 4 / 5 --------------------------------------------------------------------
async def test_prefix_only_and_suffix_only_similarity_do_not_alert(make_app):
    h = await make_app()
    h.history(payments=8)
    await h.add()
    await h.go_live()
    await h.send_live(A.VICTIM, A.PREFIX_ONLY, 20_000 * USDT)
    await h.send_live(A.VICTIM, A.SUFFIX_ONLY, 20_000 * USDT)
    await h.settle()
    assert await h.events() == []


# 6 / 7 --------------------------------------------------------------------
async def test_victim_payment_to_lookalike_is_successful_event_with_alert(make_app):
    h = await make_app()
    h.history(payments=8)
    await h.add()
    await h.go_live()
    tx = await h.send_live(A.VICTIM, A.POISON, 25_000 * USDT)
    # Alert is queued in the same transaction as the incident and delivered immediately.
    await h.app.alerts.deliver_due()
    [ev] = await h.events()
    assert ev.event_type == SUCCESS
    assert ev.tx_hash == tx
    assert ev.legitimate_recipient == A.LEGIT and ev.suspicious_recipient == A.POISON
    assert ev.confidence >= 80
    assert re.fullmatch(r"TRON-POISON-\d{8}-\d{6}", ev.case_id)
    assert ev.legit_tx_count == 8 and ev.suspicious_prior_tx_count == 0
    assert ev.detection_latency_ms is not None and ev.alert_latency_ms is not None
    [msg] = h.sent("SUCCESSFUL ADDRESS POISONING DETECTED")
    assert f"https://tronscan.org/#/transaction/{tx}" in msg["text"]
    assert f"https://tronscan.org/#/address/{A.POISON}" in msg["text"]
    assert "POSSIBLE SUCCESSFUL ADDRESS-POISONING ATTACK" in msg["text"]
    buttons = [b["text"] for row in msg["reply_markup"]["inline_keyboard"] for b in row]
    assert buttons == ["📋 COPY CASE", "🔎 TRACE FUNDS", "📄 FULL REPORT", "🐦 PREPARE X POST"]
    async with h.app.sf() as s:
        m = (await s.execute(select(AddressSimilarityMatch).where(AddressSimilarityMatch.transaction_id == ev.transaction_id))).scalar_one()
    assert (m.prefix_match_length, m.suffix_match_length) == (5, 4)
    # suspicious recipient is now recorded, but flagged
    r = await recipient(h, A.VICTIM, A.POISON)
    assert r.transaction_count == 1 and r.flagged_suspicious


# 8 / 9 --------------------------------------------------------------------
async def test_dust_is_supporting_evidence_not_a_requirement(make_app):
    h = await make_app()
    h.history(victim=A.VICTIM, payments=8)
    h.history(victim=A.VICTIM_2, payments=8)
    # VICTIM_2 received a 0.000001 USDT dust transfer from the look-alike before monitoring started
    h.chain.send(A.POISON, A.VICTIM_2, 1, ts_ms=h.now_ms - 2 * 86_400_000)
    await h.add(A.VICTIM)
    await h.add(A.VICTIM_2)
    attempts = await h.events(event_type=ATTEMPT, victim_wallet=A.VICTIM_2)
    assert len(attempts) == 1 and attempts[0].is_historical  # recorded, not alerted
    assert h.sent("ATTEMPT") == []
    await h.go_live()

    # 9: no dust observed -> still a successful event
    await h.send_live(A.VICTIM, A.POISON, 25_000 * USDT)
    [ev_nodust] = await h.events(event_type=SUCCESS, victim_wallet=A.VICTIM)
    assert ev_nodust.poisoning_tx_observed == "NO"

    # 8: dust observed -> supporting evidence, higher confidence
    await h.send_live(A.VICTIM_2, A.POISON, 25_000 * USDT)
    [ev_dust] = await h.events(event_type=SUCCESS, victim_wallet=A.VICTIM_2)
    assert ev_dust.poisoning_tx_observed == "YES"
    assert ev_dust.fast_confidence > ev_nodust.fast_confidence
    ev = await h.evidence(ev_dust.id)
    assert any(e.evidence_type == "PRIOR_DUST" and e.kind == "FACT" and e.tx_hash for e in ev)


# 10 ------------------------------------------------------------------------
async def test_duplicate_transaction_never_duplicates_incident_or_alert(make_app):
    h = await make_app()
    h.history(payments=8)
    await h.add()
    await h.go_live()
    await h.send_live(A.VICTIM, A.POISON, 25_000 * USDT)
    await h.settle()
    # API replay: re-process the same block, plus the same transfers via the account endpoint (gap fill / polling overlap)
    blk = h.chain.blocks[h.chain.head]
    await h.app.ingestor.ingest(blk.transfers, source="LIVE")
    await h.app.monitor.process_block(blk)
    await h.app.ingestor.poll_wallet(A.VICTIM, h.now_ms - 3_600_000, source="GAPFILL")
    await h.app.ingestor.recover_pending()
    await h.settle()
    assert len(await h.events(event_type=SUCCESS)) == 1
    assert len(await h.alerts(alert_type="SUCCESS")) == 1
    assert len(h.sent("SUCCESSFUL ADDRESS POISONING DETECTED")) == 1
    async with h.app.sf() as s:
        n = len((await s.execute(select(Transaction).where(Transaction.tx_hash == blk.transfers[0].tx_hash))).scalars().all())
    assert n == 1
    assert (await recipient(h, A.VICTIM, A.POISON)).transaction_count == 1


# 15 ------------------------------------------------------------------------
async def test_large_amount_exact_integer_handling(make_app):
    h = await make_app()
    h.history(payments=8)
    await h.add()
    await h.go_live()
    big = 2_000_000_000 * USDT + 123_456  # 2 billion USDT and 0.123456
    await h.send_live(A.VICTIM, A.POISON, big)
    [ev] = await h.events(event_type=SUCCESS)
    assert ev.amount == big and isinstance(ev.amount, int)
    await h.app.alerts.deliver_due()
    assert "2,000,000,000.123456 USDT" in h.sent("SUCCESSFUL")[0]["text"]


# 16 ------------------------------------------------------------------------
async def test_very_small_amount_is_candidate_not_successful(make_app):
    h = await make_app()
    h.history(payments=8)
    await h.add()
    await h.go_live()
    await h.send_live(A.VICTIM, A.POISON, 500_000)  # 0.5 USDT
    await h.settle()
    [ev] = await h.events()
    assert ev.event_type == CANDIDATE  # never SUCCESSFUL below MIN_VICTIM_AMOUNT_USDT
    assert h.sent("SUCCESSFUL") == []  # candidates are not pushed by default


async def test_candidate_upgraded_by_investigation(make_app):
    h = await make_app()
    # weak relationship: only 2 small payments -> fast score below threshold
    h.history(payments=2, amount=100 * USDT)
    # attacker campaign: dust to many wallets (not to the victim)
    for w in A.DUSTED_WALLETS:
        h.chain.send(A.POISON, w, 1, ts_ms=h.chain.head_ts + 3000)
    await h.add()
    await h.go_live()
    await h.send_live(A.VICTIM, A.POISON, 150 * USDT)
    [ev] = await h.events()
    assert ev.event_type == CANDIDATE
    h.chain.send(A.POISON, A.HOPS[0], 149 * USDT, ts_ms=h.chain.head_ts + 3000)  # fast forwarding
    await h.settle()
    [ev] = await h.events()
    assert ev.confidence > ev.fast_confidence
    ev_rows = await h.evidence(ev.id)
    assert any(e.evidence_type == "MULTI_VICTIM_DUST" for e in ev_rows)


# 17 ------------------------------------------------------------------------
async def test_multiple_victims_same_suspicious_address(make_app):
    h = await make_app()
    h.history(victim=A.VICTIM, payments=6)
    h.history(victim=A.VICTIM_2, payments=6)
    await h.add(A.VICTIM)
    await h.add(A.VICTIM_2)
    await h.go_live()
    await h.send_live(A.VICTIM, A.POISON, 10_000 * USDT)
    await h.send_live(A.VICTIM_2, A.POISON, 30_000 * USDT)
    await h.settle()
    events = await h.events(event_type=SUCCESS)
    assert {e.victim_wallet for e in events} == {A.VICTIM, A.VICTIM_2}
    for e in events:
        types = {x.evidence_type for x in await h.evidence(e.id)}
        assert "OTHER_VICTIMS" in types, e.victim_wallet


# 18 ------------------------------------------------------------------------
async def test_forwarding_is_traced_and_recorded(make_app):
    h = await make_app()
    h.history(payments=8)
    await h.add()
    await h.go_live()
    await h.send_live(A.VICTIM, A.POISON, 25_000 * USDT)
    await h.app.alerts.deliver_due()
    h.chain.label(A.EXCHANGE, "Binance-Hot 7 (test tag)", "exchange")
    t = h.chain.head_ts
    path = [A.POISON, *A.HOPS, A.EXCHANGE]
    for i, (a, b) in enumerate(zip(path, path[1:])):
        h.chain.send(a, b, (24_000 - i) * USDT, ts_ms=t + (i + 1) * 30_000)
    await h.settle()
    [ev] = await h.events(event_type=SUCCESS)
    from app.services import report_service as rs

    async with h.app.sf() as s:
        b = await rs.load_bundle(s, ev.id)
    hops = b.trace_hops()
    assert [x.hop for x in hops] == [1, 2, 3, 4]
    assert hops[-1].to_address == A.EXCHANGE and hops[-1].to_label.startswith("Binance-Hot")
    assert "possible exchange/service attribution" in hops[-1].terminal_reason
    assert all(x.block_number for x in hops)
    assert ev.forwarding_summary and "forwarded" in ev.forwarding_summary
    assert h.sent("FUND TRACE")
    assert "possible exchange/service attribution" in rs.trace_message(b)


# 19 ------------------------------------------------------------------------
async def test_multiple_lookalike_recipients(make_app):
    h = await make_app()
    h.history(payments=6, extra=[(A.LEGIT_B, 9_000 * USDT)] * 5)
    await h.add()
    await h.go_live()
    await h.send_live(A.VICTIM, A.POISON, 20_000 * USDT)
    await h.send_live(A.VICTIM, A.POISON_2, 20_000 * USDT)
    await h.send_live(A.VICTIM, A.POISON_B, 9_000 * USDT)
    await h.settle()
    pairs = {(e.suspicious_recipient, e.legitimate_recipient) for e in await h.events(event_type=SUCCESS)}
    assert pairs == {(A.POISON, A.LEGIT), (A.POISON_2, A.LEGIT), (A.POISON_B, A.LEGIT_B)}


# 20 ------------------------------------------------------------------------
async def test_different_token_is_ignored(make_app):
    h = await make_app()
    h.history(payments=8)
    await h.add()
    await h.go_live()
    other_token = address_from_seed("fake-usdt-token")
    await h.send_live(A.VICTIM, A.POISON, 25_000 * USDT, token=other_token)
    await h.settle()
    assert await h.events() == []
    async with h.app.sf() as s:
        assert (await s.execute(select(Transaction).where(Transaction.token_contract == other_token))).first() is None


# extra scenarios ------------------------------------------------------------
async def test_zero_value_transfer_from_spoof_is_attempt_not_payment(make_app):
    h = await make_app()
    h.history(payments=6)
    await h.add()
    await h.go_live()
    attacker = address_from_seed("attacker-signer")
    await h.send_live(A.VICTIM, A.POISON, 0, initiator=attacker)  # transferFrom(victim, lookalike, 0)
    await h.settle()
    [ev] = await h.events()
    assert ev.event_type == ATTEMPT
    assert await recipient(h, A.VICTIM, A.POISON) is None  # not counted as a payment
    assert h.sent("SUCCESSFUL") == []


async def test_third_party_initiated_transfer_is_not_successful(make_app):
    h = await make_app()
    h.history(payments=8)
    await h.add()
    await h.go_live()
    await h.send_live(A.VICTIM, A.POISON, 25_000 * USDT, initiator=address_from_seed("spender"))
    [ev] = await h.events()
    assert ev.event_type == CANDIDATE


async def test_historical_poisoning_found_by_retrospective_scan(make_app):
    h = await make_app()
    h.history(payments=6, extra=[(A.POISON, 12_000 * USDT)])
    await h.add()
    [ev] = await h.events(event_type=SUCCESS)
    assert ev.is_historical
    assert h.sent("HISTORY SCAN COMPLETE")
    assert "1 possible successful poisoning event" in h.sent("HISTORY SCAN COMPLETE")[0]["text"]
    assert h.sent("SUCCESSFUL ADDRESS POISONING DETECTED") == []
    # a later payment to the same (now flagged) address is still analysed
    await h.go_live()
    await h.send_live(A.VICTIM, A.POISON, 5_000 * USDT)
    assert len(await h.events(event_type=SUCCESS)) == 2


async def test_paused_wallet_records_but_does_not_alert(make_app):
    from app.domain import WalletStatus

    h = await make_app()
    h.history(payments=8)
    await h.add()
    await h.app.admin.set_wallet_status(A.VICTIM, WalletStatus.PAUSED)
    await h.go_live()
    await h.send_live(A.VICTIM, A.POISON, 25_000 * USDT)
    await h.settle()
    assert len(await h.events(event_type=SUCCESS)) == 1
    assert h.sent("SUCCESSFUL") == []


async def test_global_pause_holds_alerts_until_resume(make_app):
    h = await make_app()
    h.history(payments=8)
    await h.add()
    await h.app.admin.set_global_pause(True)
    await h.go_live()
    await h.send_live(A.VICTIM, A.POISON, 25_000 * USDT)
    await h.app.alerts.deliver_due()
    assert h.sent("SUCCESSFUL") == []
    await h.app.admin.set_global_pause(False)
    await h.app.alerts.deliver_due()
    assert len(h.sent("SUCCESSFUL")) == 1


async def test_live_payment_while_history_scan_pending_is_still_detected(make_app):
    h = await make_app()
    h.history(payments=8)
    # wallet added but history job not yet run
    await h.app.admin.add_wallet(A.VICTIM)
    await h.go_live()
    await h.send_live(A.VICTIM, A.POISON, 25_000 * USDT)
    assert await h.events() == []  # nothing known yet -> no claim without evidence
    await h.settle()  # history completes; the retrospective re-checks the live payment
    r = await recipient(h, A.VICTIM, A.LEGIT)
    assert r.transaction_count == 8
    [ev] = await h.events(event_type=SUCCESS)
    assert not ev.is_historical
    assert len(h.sent("SUCCESSFUL ADDRESS POISONING DETECTED")) == 1
