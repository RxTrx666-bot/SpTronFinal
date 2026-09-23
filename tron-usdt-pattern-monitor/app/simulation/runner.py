"""Simulation mode: ``python -m app.main simulate``.

Runs the REAL collector -> parser -> pipeline -> pattern engine -> watchlist ->
matcher -> alert dispatcher against a scripted in-memory TRON event stream and
an SQLite database, and verifies every step.  No network access is needed.

Part 1 - historical backfill discovers three independent relationships with
         very different test sizes (5-10, 200-300 and 950-1,100 USDT) while
         rejecting dust, random small transfers, one-off sequences and noise.
Part 2 - live stream: the learned tests re-appear and produce RED alerts
         immediately (before any large transfer exists), followed by the large
         follow-up alert and a model update (3 -> 4 sequences).
Part 3 - fully real-time learning of TEST -> LARGE -> TEST -> LARGE -> TEST
         with no prior history (MIN_SUCCESSFUL_SEQUENCES=2): the final TEST
         produces the RED alert before the next LARGE is sent.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app.amounts import fmt_range, fmt_usdt
from app.clock import ManualClock, SystemClock
from app.collector.address import address_from_seed, short
from app.config.settings import Settings
from sqlalchemy import select

from app.database import repository as repo
from app.database.models import Alert, Transaction
from app.domain import datetime_to_ms
from app.main import Application
from app.simulation.source import SimulatedEventSource, make_event, tx_hash
from app.telegram.alerts import AlertSink, CompositeSink, ConsoleSink, RecordingSink

W = {name: address_from_seed(f"sim-wallet-{name}") for name in "ABCDEFMNPQXYZ"}
OTHER_TOKEN = address_from_seed("some-other-trc20-token")


class Sim:
    def __init__(self, app: Application, src: SimulatedEventSource, rec: RecordingSink, contract: str) -> None:
        self.app, self.src, self.rec, self.contract = app, src, rec, contract
        self.failures: list[str] = []

    def check(self, cond: bool, what: str) -> None:
        print(f"   {'✅' if cond else '❌'} {what}")
        if not cond:
            self.failures.append(what)

    def event(self, s: str, r: str, amount, when: datetime, *, unconfirmed=False, txid=None) -> dict:
        return self.src.add(
            make_event(
                sender=W[s], recipient=W[r], amount_usdt=amount, ts_ms=datetime_to_ms(when),
                contract=self.contract, txid=txid, unconfirmed=unconfirmed,
            )
        )

    async def settle(self) -> list[str]:
        """Let learning + alert delivery finish; return newly delivered messages."""
        before = len(self.rec.messages)
        await self.app.analysis.drain()
        await self.app.dispatcher.flush()
        return self.rec.messages[before:]

    async def poll(self, *, unconfirmed=False) -> list[str]:
        if unconfirmed:
            await self.app.collector.poll_unconfirmed_once()
        else:
            while await self.app.collector.poll_confirmed_once():
                pass
        return await self.settle()

    async def entry(self, s: str, r: str):
        async with self.app.session_factory() as ss:
            return await repo.get_watchlist(ss, W[s], W[r])


def _types(msgs: list[str]) -> list[str]:
    out = []
    for m in msgs:
        if "WATCHLIST TEST TRANSFER DETECTED" in m:
            out.append("TEST")
        elif "LARGE FOLLOW-UP DETECTED" in m:
            out.append("FOLLOWUP")
        elif "AUTOMATIC WATCHLIST ACTIVATED" in m:
            out.append("ACTIVATED")
        elif "NEW USDT TEST" in m:
            out.append("NEW_PATTERN")
        else:
            out.append("OTHER")
    return out


def _settings(database_url: str, **kw) -> Settings:
    base = dict(
        database_url=database_url,
        initial_history_days=10,
        backfill_window_seconds=3600,
        send_startup_message=False,
        enable_unconfirmed=True,
        telegram_bot_token="",
        telegram_chat_id="",
        heartbeat_file="/tmp/tron-usdt-sim.heartbeat",
    )
    base.update(kw)
    return Settings(_env_file=None, **base)


def _banner(text: str) -> None:
    print("\n" + "█" * 72 + f"\n  {text}\n" + "█" * 72)


async def part1_and_2(database_url: str, extra_sink: AlertSink | None) -> list[str]:
    settings = _settings(database_url)
    src = SimulatedEventSource()
    rec = RecordingSink()
    sink = CompositeSink(rec, extra_sink or ConsoleSink())
    app = Application(settings, source=src, sink=sink, clock=SystemClock())
    sim = Sim(app, src, rec, settings.usdt_contract_address)
    now = datetime.now(timezone.utc)
    d = lambda days, minutes=0: now - timedelta(days=days) + timedelta(minutes=minutes)  # noqa: E731

    _banner("PART 1 - HISTORICAL BACKFILL (10 days of simulated USDT transfers)")
    # Relationship 1: A -> B   5-10 USDT tests
    for day, test, large, mins in ((6, 5, 20_000, 20), (4, 5, 30_000, 18), (2, 10, 40_000, 25)):
        sim.event("A", "B", test, d(day)); sim.event("A", "B", large, d(day, mins))
    # Relationship 2: C -> D   200-300 USDT tests
    for day, test, large, mins in ((7, 250, 40_000, 40), (5, 300, 50_000, 35), (3, 200, 35_000, 30)):
        sim.event("C", "D", test, d(day)); sim.event("C", "D", large, d(day, mins))
    # Relationship 3: E -> F   950-1,100 USDT tests
    for day, test, large, mins in ((8, 1000, 100_000, 60), (5.5, 1100, 120_000, 50), (2.5, 950, 80_000, 45)):
        sim.event("E", "F", test, d(day)); sim.event("E", "F", large, d(day, mins))
    # Noise that must NOT create a watchlist entry:
    for i in range(6):  # address-poisoning dust into B and A
        sim.event("P", "B", "0.000001", d(9 - i, 7)); sim.event("P", "A", "0.000001", d(9 - i, 9))
    for i, amt in enumerate((5, 12, 7, 3, 9, 6)):  # random small transfers, never followed by a large one
        sim.event("X", "Y", amt, d(9 - i * 1.4))
    sim.event("Z", "B", 5, d(6, 3)); sim.event("Z", "B", 20_000, d(6, 30))  # one-off sequence (1 only)
    for i, amt in enumerate((50, 3000, 20, 40_000, 700, 90, 12_000, 5, 800, 60_000)):  # irregular business flow
        sim.event("M", "N", amt, d(9 - i * 0.9))
    # Other token + a TRX-style payload in the stream: must be rejected by the decoder.
    src.add(make_event(sender=W["A"], recipient=W["B"], amount_usdt=5, ts_ms=datetime_to_ms(d(1)), contract=OTHER_TOKEN))
    src.confirmed.append({"transaction_id": tx_hash(), "block_timestamp": datetime_to_ms(d(1)), "type": "TransferContract",
                          "amount": 5_000_000, "owner_address": W["A"], "to_address": W["B"]})
    src.confirmed.append({"garbage": True, "block_timestamp": datetime_to_ms(d(1))})

    await app.start()
    await app.collector.run_backfill(app.stop_event, app.on_backfill_complete)
    await app.dispatcher.flush()

    print("\n📋 Automatic watchlist after backfill:")
    for snap in sorted(app.cache.values(), key=lambda s: s.sender):
        print(
            f"   {short(snap.sender)} → {short(snap.recipient)}  {snap.status.value:<9} {snap.confidence:<6}"
            f" test {fmt_range(snap.test_min_raw, snap.test_max_raw)} → large "
            f"{fmt_range(snap.large_min_raw, snap.large_max_raw, compact=True)} USDT, {snap.successful_sequences} seq,"
            f" match band {fmt_usdt(snap.match_low_raw)}–{fmt_usdt(snap.match_high_raw)}"
        )
    for s, r, label in (("A", "B", "5-10"), ("C", "D", "200-300"), ("E", "F", "950-1,100")):
        e = await sim.entry(s, r)
        sim.check(e is not None and e.status == "ACTIVE", f"{s}→{r} ({label} USDT tests) automatically ACTIVE on the watchlist")
    ab = await sim.entry("A", "B")
    sim.check(ab is not None and ab.test_amount_min_raw == 5_000_000 and ab.test_amount_max_raw == 10_000_000,
              "A→B learned test range is exactly 5–10 USDT")
    cd = await sim.entry("C", "D")
    sim.check(cd is not None and cd.test_amount_min_raw == 200_000_000 and cd.test_amount_max_raw == 300_000_000,
              "C→D learned test range is 200–300 USDT (not rejected as 'too big for a test')")
    for s, r, what in (("P", "B", "dust sender"), ("X", "Y", "random small transfers"), ("Z", "B", "one-off sequence"),
                       ("M", "N", "irregular business flow")):
        e = await sim.entry(s, r)
        sim.check(e is None or e.status != "ACTIVE", f"{what} ({s}→{r}) NOT on the active watchlist")
    rej = app.collector.stats.rejected
    sim.check(rej.get("not_usdt_contract", 0) >= 1 and rej.get("no_contract_address", 0) >= 1,
              f"non-USDT token and TRX payloads rejected ({rej})")
    sim.check(len(app.cache) >= 3, "watchlist built with zero manual wallet input")

    _banner("PART 2 - LIVE STREAM: known test transfers re-appear")
    print("\n▶ A→B sends 5 USDT (UNCONFIRMED, seen in the mempool/unsolidified block) - NO large transfer exists yet")
    t_txid = tx_hash("live-A-B-test")
    sim.event("A", "B", 5, datetime.now(timezone.utc), unconfirmed=True, txid=t_txid)
    msgs = await sim.poll(unconfirmed=True)
    sim.check(_types(msgs) == ["TEST"], "RED WATCHLIST TEST ALERT delivered immediately")
    large_exists = any(r["transaction_id"] != t_txid and r["block_timestamp"] >= datetime_to_ms(now)
                       and r["result"]["value"] == "20000000000" for r in src.confirmed + src.unconfirmed if "result" in r)
    sim.check(not large_exists, "alert was sent BEFORE the large transfer happened")
    async with app.session_factory() as ss:
        lat = [a for a in (await ss.execute(select(Alert).where(Alert.alert_type == "TEST_DETECTED"))).scalars()]
    if lat:
        a = lat[-1]
        print(f"   ⏱  measured latency: block time → Telegram delivered = {a.total_detection_latency_ms} ms "
              f"(processing {int((a.processing_end_time - a.processing_start_time).total_seconds() * 1000)} ms, "
              f"send {int((a.telegram_send_end_time - a.telegram_send_start_time).total_seconds() * 1000)} ms)")

    print("\n▶ The same 5 USDT transfer is solidified (CONFIRMED)")
    src.confirm(t_txid)
    msgs = await sim.poll()
    sim.check(msgs == [], "confirmation updates the row without a second test alert")
    async with app.session_factory() as ss:
        row = (await ss.execute(select(Transaction).where(Transaction.transaction_hash == t_txid))).scalar_one()
    sim.check(row.status == "CONFIRMED" and row.confirmed_at is not None, "transaction status UNCONFIRMED → CONFIRMED")

    print("\n▶ API delivers the same test transfer again (duplicate delivery)")
    src.confirmed.append(dict(src.confirmed[-1]))
    msgs = await sim.poll()
    sim.check(msgs == [], "duplicate delivery produced no duplicate alert")

    print("\n▶ A→B now sends 20,000 USDT")
    sim.event("A", "B", 20_000, datetime.now(timezone.utc) + timedelta(seconds=1))
    msgs = await sim.poll()
    sim.check("FOLLOWUP" in _types(msgs), "LARGE FOLLOW-UP ALERT delivered")
    ab = await sim.entry("A", "B")
    sim.check(ab.successful_sequences == 4, f"pattern model updated: successful sequences 3 → {ab.successful_sequences}")

    print("\n▶ C→D sends 250 USDT and E→F sends 1,000 USDT")
    sim.event("C", "D", 250, datetime.now(timezone.utc))
    sim.event("E", "F", 1000, datetime.now(timezone.utc))
    msgs = await sim.poll()
    sim.check(_types(msgs).count("TEST") == 2, "both learned tests (250 and 1,000 USDT) produced RED alerts")

    print("\n▶ Noise: dust to B, a new sender's 5 USDT to B, A's 5 USDT to a DIFFERENT recipient, A→B 700 USDT")
    sim.event("P", "B", "0.000001", datetime.now(timezone.utc))
    sim.event("Q", "B", 5, datetime.now(timezone.utc))
    sim.event("A", "C", 5, datetime.now(timezone.utc))
    sim.event("A", "B", 700, datetime.now(timezone.utc))
    msgs = await sim.poll()
    sim.check("TEST" not in _types(msgs), "no alert: relationships are independent, amounts outside learned band ignored")

    await app.close()
    return sim.failures


async def part3(database_url: str, extra_sink: AlertSink | None) -> list[str]:
    _banner("PART 3 - REAL-TIME LEARNING: TEST → LARGE → TEST → LARGE → TEST")
    clock = ManualClock(datetime.now(timezone.utc) - timedelta(hours=3))
    settings = _settings(database_url, initial_history_days=0, min_successful_sequences=2, enable_unconfirmed=False)
    src = SimulatedEventSource()
    rec = RecordingSink()
    app = Application(settings, source=src, sink=CompositeSink(rec, extra_sink or ConsoleSink()), clock=clock)
    sim = Sim(app, src, rec, settings.usdt_contract_address)
    await app.start()
    print("   (MIN_SUCCESSFUL_SEQUENCES=2 so the 5-step sequence can activate; the default is 3)")
    steps = [("TEST", 5, 0), ("LARGE", 20_000, 20), ("TEST", 5, 40), ("LARGE", 30_000, 58), ("TEST", 10, 90)]
    for label, amount, minute in steps:
        when = clock.now()
        print(f"\n▶ t+{minute:>3} min  A→B {label:<5} {amount:,} USDT")
        sim.event("A", "B", amount, when)
        msgs = await sim.poll()
        print(f"   alerts: {_types(msgs) or 'none'}")
        if label == "TEST" and minute == 90:
            sim.check(_types(msgs) == ["TEST"], "final TEST immediately produced the RED WATCHLIST ALERT")
            sim.check(True, "…and the next LARGE transfer has not been sent yet")
        if minute == 58:
            e = await sim.entry("A", "B")
            sim.check(e is not None and e.status == "ACTIVE", "relationship learned in real time and automatically ACTIVE")
        nxt = next((m for _, _, m in steps if m > minute), minute + 20)
        clock.advance(minutes=nxt - minute)
    print("\n▶ t+105 min  A→B LARGE 40,000 USDT")
    sim.event("A", "B", 40_000, clock.now())
    msgs = await sim.poll()
    sim.check("FOLLOWUP" in _types(msgs), "LARGE FOLLOW-UP ALERT delivered")
    e = await sim.entry("A", "B")
    sim.check(e.successful_sequences == 3, f"model updated to {e.successful_sequences} sequences")
    await app.close()
    return sim.failures


async def run_simulation(database_url: str = "sqlite+aiosqlite:///:memory:", use_telegram: bool = False) -> int:
    extra: AlertSink | None = None
    tg = None
    if use_telegram:
        from app.config.settings import get_settings
        from app.telegram.bot import TelegramClient, TelegramSink

        s = get_settings()
        if not s.telegram_enabled:
            print("--telegram requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
            return 2
        tg = TelegramClient(s.telegram_bot_token)
        extra = CompositeSink(ConsoleSink(), TelegramSink(tg, s.telegram_chat_id))
    failures = await part1_and_2(database_url, extra)
    url3 = database_url if ":memory:" in database_url else database_url.replace(".db", "-part3.db")
    failures += await part3(url3, extra)
    if tg:
        await tg.close()
    _banner("SIMULATION RESULT")
    if failures:
        print(f"❌ {len(failures)} check(s) failed:")
        for f in failures:
            print("   -", f)
        return 1
    print("✅ All simulation checks passed.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(asyncio.run(run_simulation()))

