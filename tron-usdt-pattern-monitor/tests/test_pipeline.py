"""End-to-end tests through the real collector, pipeline, DB, watchlist and dispatcher."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.database.models import Alert, PatternSequence, TestEvent, WalletPair, WatchlistEntry
from app.simulation.source import tx_hash
from tests.helpers import addr, kinds

AB = {("A", "B"): [(5, 20_000, 20), (5, 30_000, 18), (10, 40_000, 25)]}
CD = {("C", "D"): [(250, 40_000, 40), (300, 50_000, 35), (200, 35_000, 30)]}
EF = {("E", "F"): [(1000, 100_000, 60), (1100, 120_000, 50), (950, 80_000, 45)]}


async def learned(make_harness, *patterns, **kw):
    h = await make_harness(**kw)
    start = h.clock.now() - timedelta(days=8)
    for p in patterns:
        await h.history(p, start)
    await h.backfill(start - timedelta(hours=1))
    return h


# 9 + 10 + 23: historical backfill builds the watchlist automatically, no manual input
async def test_backfill_creates_watchlist_automatically(make_harness):
    h = await learned(make_harness, AB, CD, EF)
    for s, r in (("A", "B"), ("C", "D"), ("E", "F")):
        e = await h.entry(s, r)
        assert e is not None and e.status == "ACTIVE" and e.confidence == "HIGH"
        assert e.successful_sequences == 3
    msgs = h.rec.messages
    assert any("HISTORICAL BACKFILL COMPLETE" in m for m in msgs)
    assert "TEST" not in kinds(msgs)  # history never produces live test alerts
    assert not hasattr(h.app, "add_wallet")  # there is no manual wallet API at all


# 11/12/13 through the full stack + 17: test alert generation
@pytest.mark.parametrize(
    "pattern,pair,amount",
    [(AB, ("A", "B"), 5), (CD, ("C", "D"), 250), (EF, ("E", "F"), 1000)],
    ids=["5->20K", "250->40K", "1000->100K"],
)
async def test_known_test_triggers_red_alert(make_harness, pattern, pair, amount):
    h = await learned(make_harness, pattern)
    h.clock.advance(minutes=5)
    h.ev(*pair, amount, h.clock.now())
    msgs = await h.poll()
    assert kinds(msgs) == ["TEST"]
    assert "WATCHLIST TEST TRANSFER DETECTED" in msgs[0] and addr(pair[0]) in msgs[0] and addr(pair[1]) in msgs[0]
    a = (await h.alerts("TEST_DETECTED"))[0]
    assert a.total_detection_latency_ms is not None and a.telegram_send_end_time is not None
    assert a.processing_start_time <= a.processing_end_time


# 18 + 19 + 24: alert before large, follow-up detection, model update 3 -> 4
async def test_alert_before_large_then_followup_and_learning(make_harness):
    h = await learned(make_harness, AB)
    h.clock.advance(minutes=5)
    h.ev("A", "B", 5, h.clock.now())
    msgs = await h.poll()
    assert kinds(msgs) == ["TEST"]
    test_alert = (await h.alerts("TEST_DETECTED"))[0]
    assert test_alert.sent_at is not None
    assert await h.tx_count() == 7  # the large transfer does not exist yet

    h.clock.advance(minutes=15)
    h.ev("A", "B", 20_000, h.clock.now())
    msgs = await h.poll()
    assert "FOLLOWUP" in kinds(msgs)
    fu = (await h.alerts("LARGE_FOLLOWUP"))[0]
    assert fu.sent_at > test_alert.sent_at
    assert "15 minutes" in fu.message_text and "5 USDT" in fu.message_text
    e = await h.entry("A", "B")
    assert e.successful_sequences == 4
    async with h.app.session_factory() as s:
        te = (await s.execute(select(TestEvent))).scalar_one()
    assert te.status == "FOLLOWED_UP"


# 6 + 14: relationship matching and independence
async def test_relationships_are_independent(make_harness):
    h = await learned(make_harness, AB, {("A", "C"): [(500, 50_000, 30), (500, 60_000, 30), (450, 55_000, 30)]})
    assert (await h.entry("A", "C")).test_amount_min_raw == 450_000_000
    h.clock.advance(minutes=5)
    h.ev("A", "C", 5, h.clock.now())  # A's 5 USDT test belongs to A->B, not A->C
    h.ev("X", "B", 5, h.clock.now())  # another sender to B
    h.ev("B", "A", 5, h.clock.now())  # reversed direction
    assert kinds(await h.poll()) == []
    h.ev("A", "C", 480, h.clock.now())
    msgs = await h.poll()
    assert kinds(msgs) == ["TEST"] and addr("C") in msgs[0]


# 15 + 16: dust and random small transfers never alert
async def test_dust_and_random_small_transfers_never_alert(make_harness):
    h = await learned(make_harness, AB)
    h.clock.advance(minutes=5)
    h.ev("A", "B", "0.000001", h.clock.now())
    h.ev("A", "B", 1, h.clock.now())
    h.ev("P", "B", "0.000001", h.clock.now())
    for i in range(10):
        h.ev("R", "S", 5, h.clock.now() + timedelta(seconds=i))
    assert kinds(await h.poll()) == []
    assert await h.entry("R", "S") is None


# 20: duplicate transaction prevention
async def test_duplicate_transactions_stored_once(make_harness):
    h = await make_harness()
    raw = h.ev("A", "B", 5, h.clock.now())
    h.src.confirmed.append(dict(raw))
    h.src.confirmed.append(dict(raw))
    await h.poll()
    await h.poll()
    h.app.pipeline.seen = type(h.app.pipeline.seen)()  # forget in-memory cache: DB must still dedupe
    await h.poll()
    assert await h.tx_count() == 1
    async with h.app.session_factory() as s:
        wp = (await s.execute(select(WalletPair))).scalar_one()
    assert wp.total_transfers == 1  # statistics not double counted


# 21: duplicate alert prevention
async def test_duplicate_alert_prevention(make_harness):
    h = await learned(make_harness, AB)
    h.clock.advance(minutes=5)
    txid = tx_hash("dup-test")
    raw = h.ev("A", "B", 5, h.clock.now(), txid=txid)
    await h.poll()
    h.src.confirmed.append(dict(raw))
    h.app.pipeline.seen = type(h.app.pipeline.seen)()
    await h.poll()
    # force re-matching of the same transfer (e.g. recovery after a crash)
    stored = await h.tx(txid)
    from app.database import repository as repo

    async with h.app.session_factory() as s:
        st = await repo.get_transaction(s, stored.id)
    await h.app.matcher.match(st, h.clock.now())
    await h.app.matcher.match(st, h.clock.now())
    await h.app.dispatcher.flush()
    assert len(await h.alerts("TEST_DETECTED")) == 1
    assert kinds(h.rec.messages).count("TEST") == 1


# 22: restart recovery (persisted watchlist, cursor, outbox)
async def test_restart_recovery(make_harness, db_url):
    if ":memory:" in db_url:
        pytest.skip("needs a persistent database")
    h = await learned(make_harness, AB)
    cursor_before = h.app.collector.stats.confirmed_cursor_ms
    await h.app.close()

    # second process on the same database
    h2 = await make_harness(clock=h.clock, fresh=False)
    assert h2.app.collector.stats.confirmed_cursor_ms == cursor_before  # resumed, not reset
    assert h2.app.cache.get(addr("A"), addr("B")).status.value == "ACTIVE"
    h2.clock.advance(minutes=5)
    h2.ev("A", "B", 5, h2.clock.now())
    assert kinds(await h2.poll()) == ["TEST"]


async def test_crash_between_store_and_match_is_recovered(make_harness):
    h = await learned(make_harness, AB)
    h.clock.advance(minutes=5)

    async def crash(*a, **k):
        raise ConnectionError("process died")

    real = h.app.matcher._match
    h.app.matcher._match = crash
    h.ev("A", "B", 5, h.clock.now())
    with pytest.raises(ConnectionError):
        await h.app.collector.poll_confirmed_once()
    h.app.matcher._match = real
    h.clock.advance(seconds=10)
    await h.app.maintenance.run_once()  # recovery: processed=false rows are matched
    await h.app.dispatcher.flush()
    assert kinds(h.rec.messages).count("TEST") == 1


async def test_alert_outbox_survives_restart(make_harness, db_url):
    from app.telegram.alerts import RecordingSink

    h = await learned(make_harness, AB, sink=RecordingSink(fail_times=10_000))
    h.clock.advance(minutes=5)
    h.ev("A", "B", 5, h.clock.now())
    await h.poll()
    assert (await h.alerts("TEST_DETECTED"))[0].status == "PENDING"
    await h.app.close()
    h2 = await make_harness(clock=h.clock, fresh=False)
    await h2.app.dispatcher.flush()
    assert kinds(h2.rec.messages).count("TEST") == 1


# 27 + 28: unconfirmed handling and confirmation update
async def test_unconfirmed_then_confirmed(make_harness):
    h = await learned(make_harness, AB)
    h.clock.advance(minutes=5)
    txid = tx_hash("unconf")
    h.ev("A", "B", 5, h.clock.now(), unconfirmed=True, txid=txid)
    msgs = await h.poll(unconfirmed=True)
    assert kinds(msgs) == ["TEST"] and "UNCONFIRMED" in msgs[0]
    row = await h.tx(txid)
    assert row.status == "UNCONFIRMED" and row.confirmed_at is None
    async with h.app.session_factory() as s:
        wp = (await s.execute(select(WalletPair).where(WalletPair.sender == addr("A")))).scalar_one()
    assert wp.total_transfers == 6  # unconfirmed transfers do not feed statistics / learning

    h.src.confirm(txid)
    assert kinds(await h.poll()) == []  # no second test alert
    row = await h.tx(txid)
    assert row.status == "CONFIRMED" and row.confirmed_at is not None
    async with h.app.session_factory() as s:
        wp = (await s.execute(select(WalletPair).where(WalletPair.sender == addr("A")))).scalar_one()
    assert wp.total_transfers == 7


async def test_confirmation_alert_optional(make_harness):
    h = await learned(make_harness, AB, send_confirmation_alerts=True)
    h.clock.advance(minutes=5)
    txid = tx_hash("unconf2")
    h.ev("A", "B", 5, h.clock.now(), unconfirmed=True, txid=txid)
    await h.poll(unconfirmed=True)
    h.src.confirm(txid)
    msgs = await h.poll()
    assert any("confirmed on-chain" in m for m in msgs)
    assert kinds(h.rec.messages).count("TEST") == 1


async def test_unconfirmed_never_confirmed_is_dropped(make_harness):
    h = await learned(make_harness, AB)
    h.clock.advance(minutes=5)
    txid = tx_hash("dropped")
    h.ev("A", "B", 5, h.clock.now(), unconfirmed=True, txid=txid)
    await h.poll(unconfirmed=True)
    h.src.unconfirmed.clear()
    h.clock.advance(minutes=30)
    h.ev("Q", "W", 1, h.clock.now())  # advances the confirmed cursor
    await h.poll()
    await h.app.maintenance.run_once()
    assert (await h.tx(txid)).status == "DROPPED"
    async with h.app.session_factory() as s:
        te = (await s.execute(select(TestEvent))).scalar_one()
    assert te.status in ("CANCELLED", "EXPIRED")


# real-time learning: TEST -> LARGE -> TEST -> LARGE -> TEST
async def test_realtime_learning_sequence(make_harness):
    h = await make_harness(min_successful_sequences=2)
    got = []
    for label, amount, minutes in (("T", 5, 1), ("L", 20_000, 20), ("T", 5, 60), ("L", 30_000, 20), ("T", 10, 60)):
        h.clock.advance(minutes=minutes)
        h.ev("A", "B", amount, h.clock.now())
        got.append(kinds(await h.poll()))
    assert got[3] == ["ACTIVATED"]
    assert got[4] == ["TEST"]  # final TEST alerted before any further LARGE


async def test_candidate_then_active_notifications(make_harness):
    h = await make_harness()
    seen = []
    for amount, minutes in ((5, 1), (20_000, 20), (5, 600), (30_000, 20), (7, 600), (40_000, 20)):
        h.clock.advance(minutes=minutes)
        h.ev("A", "B", amount, h.clock.now())
        seen += kinds(await h.poll())
    assert seen == ["NEW_PATTERN", "ACTIVATED"]
    assert (await h.entry("A", "B")).status == "ACTIVE"


async def test_followup_window_expiry_weakens_success_rate(make_harness):
    h = await learned(make_harness, AB)
    e = await h.entry("A", "B")
    for _ in range(4):  # four tests that are never followed by a large transfer
        h.clock.advance(hours=3)
        h.ev("A", "B", 5, h.clock.now())
        await h.poll()
    h.clock.advance(hours=5)
    await h.app.maintenance.run_once()
    await h.app.analysis.drain()
    e2 = await h.entry("A", "B")
    async with h.app.session_factory() as s:
        n = (await s.execute(select(func.count()).select_from(TestEvent).where(TestEvent.status == "EXPIRED"))).scalar_one()
    assert n == 4
    assert e2.success_rate < e.success_rate
    assert e2.status in ("WEAKENED", "ACTIVE") and e2.confidence_score < e.confidence_score


async def test_test_flood_pauses_entry(make_harness):
    h = await learned(make_harness, AB, flood_max_tests_per_hour=3)
    for i in range(6):
        h.clock.advance(minutes=2)
        h.ev("A", "B", 5, h.clock.now())
        await h.poll()
    assert kinds(h.rec.messages).count("TEST") == 3
    assert (await h.entry("A", "B")).status == "PAUSED"


async def test_sequences_are_persisted(make_harness):
    h = await learned(make_harness, AB)
    async with h.app.session_factory() as s:
        seqs = list((await s.execute(select(PatternSequence).order_by(PatternSequence.test_timestamp))).scalars())
        n_alerts = (await s.execute(select(func.count()).select_from(Alert))).scalar_one()
        wl = (await s.execute(select(WatchlistEntry))).scalar_one()
    assert [(int(q.test_amount_raw), int(q.large_amount_raw)) for q in seqs] == [
        (5_000_000, 20_000_000_000), (5_000_000, 30_000_000_000), (10_000_000, 40_000_000_000)]
    assert str(seqs[0].amount_ratio).startswith("4000")
    assert n_alerts == 1  # backfill summary only
    assert wl.typical_test_amount_raw == 6_666_666 and wl.typical_large_amount_raw == 30_000_000_000


async def test_telegram_commands(make_harness):
    h = await learned(make_harness, AB)
    c = h.app.commands
    assert "AUTOMATIC WATCHLIST" in await c.handle("/watchlist")
    assert addr("A") in await c.handle("/watchlist 1")
    detail = await c.handle(f"/pattern {addr('A')} {addr('B')}")
    assert "PATTERN DETAIL" in detail and "5–10 USDT" in detail
    assert "Usage" in await c.handle("/pattern foo")
    assert "STATUS" in await c.handle("/status")
    assert "transactions" in await c.handle("/stats")
    assert "No wallets need to be added" in await c.handle("/help")
    assert "Paused" in await c.handle(f"/pause {addr('A')} {addr('B')}")
    h.clock.advance(minutes=5)
    h.ev("A", "B", 5, h.clock.now())
    assert kinds(await h.poll()) == []
    assert "Resumed" in await c.handle(f"/resume {addr('A')} {addr('B')}")
    h.clock.advance(minutes=5)
    h.ev("A", "B", 6, h.clock.now())
    assert kinds(await h.poll()) == ["TEST"]


def assert_telegram_html(text: str) -> None:
    """Telegram accepts only a few tags and rejects unbalanced markup."""
    from html.parser import HTMLParser

    allowed = {"b", "i", "u", "s", "code", "pre", "a", "blockquote"}

    class P(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack = []

        def handle_starttag(self, tag, attrs):
            assert tag in allowed, f"tag <{tag}> not allowed by Telegram"
            self.stack.append(tag)

        def handle_endtag(self, tag):
            assert self.stack and self.stack.pop() == tag, f"unbalanced </{tag}>"

    p = P()
    p.feed(text)
    p.close()
    assert not p.stack, f"unclosed tags {p.stack}"
    assert len(text) <= 4096


async def test_all_messages_are_valid_telegram_html(make_harness):
    h = await make_harness()
    for amount, minutes in ((5, 1), (20_000, 20), (5, 600), (30_000, 20), (7, 600), (40_000, 20), (5, 600), (25_000, 15)):
        h.clock.advance(minutes=minutes)
        h.ev("A", "B", amount, h.clock.now())
        await h.poll()
    assert set(kinds(h.rec.messages)) >= {"NEW_PATTERN", "ACTIVATED", "TEST", "FOLLOWUP"}
    c = h.app.commands
    replies = [
        await c.handle(cmd)
        for cmd in ("/help", "/status", "/watchlist", "/stats", f"/pattern {addr('A')} {addr('B')}", "/pattern x", "/nope")
    ]
    await h.app.send_system("x", h.app.formatter.startup_message(entries=1, min_seq=3, ratio="10", min_conf="HIGH"))
    await h.app.send_system("y", h.app.formatter.backfill_summary(days=7, analysed=5, snaps=h.app.cache.values()))
    await h.app.dispatcher.flush()
    for text in h.rec.messages + replies:
        assert_telegram_html(text)
