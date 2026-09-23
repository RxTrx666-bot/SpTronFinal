"""Pure behavioural-engine tests (no database)."""

import random
from datetime import datetime, timedelta, timezone

import pytest

from app.amounts import usdt_to_raw
from app.config.settings import Settings
from app.detector.followup_detector import select_followup
from app.detector.pattern_engine import HistTx, analyze_history, detect_sequences
from app.detector.test_detector import match_test_transfer
from app.domain import ConfidenceLevel, WatchlistStatus
from app.watchlist.model import WatchlistSnapshot

NOW = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)
S = Settings(_env_file=None)
U = 1_000_000  # 1 USDT in raw units


def hist(seqs, *, start_days=6, gap_hours=36, minutes=20, extra=()):
    """[(test, large), ...] -> HistTx list. ``extra`` = [(days_ago, amount)]."""
    out, i, t = [], 0, NOW - timedelta(days=start_days)
    for test, large in seqs:
        out.append(HistTx(i, f"h{i}", 0, t, usdt_to_raw(str(test)))); i += 1
        out.append(HistTx(i, f"h{i}", 0, t + timedelta(minutes=minutes), usdt_to_raw(str(large)))); i += 1
        t += timedelta(hours=gap_hours)
    for days, amount in extra:
        out.append(HistTx(i, f"h{i}", 0, NOW - timedelta(days=days), usdt_to_raw(str(amount)))); i += 1
    return out


def model(seqs, **kw):
    return analyze_history("S", "R", hist(seqs, **kw), NOW, S)


# 7. Historical sequence detection
def test_single_sequence_detected():
    seqs = detect_sequences(hist([(5, 20_000)]), ratio=S.ratio_fraction, max_followup=timedelta(hours=168))
    assert len(seqs) == 1
    assert seqs[0].test.amount_raw == 5 * U and seqs[0].large.amount_raw == 20_000 * U
    assert seqs[0].dt_seconds == 20 * 60


# 8. Multiple successful sequences
def test_multiple_sequences_detected():
    seqs = detect_sequences(
        hist([(5, 20_000), (5, 30_000), (10, 40_000)]), ratio=S.ratio_fraction, max_followup=timedelta(hours=168)
    )
    assert [(s.test.amount_raw // U, s.large.amount_raw // U) for s in seqs] == [(5, 20_000), (5, 30_000), (10, 40_000)]


def test_followup_outside_window_not_a_sequence():
    h = hist([(5, 20_000)], minutes=60 * 200)  # 200 h later > MAX_FOLLOWUP_HOURS
    assert detect_sequences(h, ratio=S.ratio_fraction, max_followup=timedelta(hours=168)) == []


def test_large_not_reused_as_test():
    # 5 -> 20,000 -> 300,000: the 20,000 is a LARGE; it must not become the test of 300,000
    h = [
        HistTx(1, "a", 0, NOW - timedelta(hours=3), 5 * U),
        HistTx(2, "b", 0, NOW - timedelta(hours=2), 20_000 * U),
        HistTx(3, "c", 0, NOW - timedelta(hours=1), 300_000 * U),
    ]
    seqs = detect_sequences(h, ratio=S.ratio_fraction, max_followup=timedelta(hours=168))
    assert len(seqs) == 1 and seqs[0].large.id == 2


# 11 / 12 / 13 - three very different test sizes, all learned
@pytest.mark.parametrize(
    "seqs,lo,hi",
    [
        ([(5, 20_000), (5, 30_000), (10, 40_000)], 5, 10),
        ([(250, 40_000), (300, 50_000), (200, 35_000)], 200, 300),
        ([(1000, 100_000), (1100, 120_000), (950, 80_000)], 950, 1100),
    ],
    ids=["5->20K", "250->40K", "1000->100K"],
)
def test_patterns_of_any_test_size_are_learned(seqs, lo, hi):
    m = model(seqs)
    assert m.qualifies_active, m.reasons
    assert m.confidence == ConfidenceLevel.HIGH
    assert (m.test_min_raw, m.test_max_raw) == (lo * U, hi * U)
    assert m.successful_sequences == 3
    # the learned band matches THIS relationship's typical test
    assert m.match_low_raw <= lo * U and m.match_high_raw >= hi * U


def test_no_global_test_amount():
    """A 1,000 USDT test is valid for one relationship and a 5 USDT test for another;
    each band excludes the other relationship's test size."""
    small, big = model([(5, 20_000), (5, 30_000), (10, 40_000)]), model([(1000, 100_000), (1100, 120_000), (950, 80_000)])
    assert small.match_high_raw < 1000 * U < big.match_high_raw
    assert big.match_low_raw > 5 * U > small.match_low_raw


# 19. No exact-amount requirement: learned from the distribution
def test_learned_range_from_distribution():
    m = model([(5, 20_000), (5, 25_000), (10, 30_000), (5, 22_000), (10, 41_000)])
    assert (m.test_min_raw, m.test_max_raw) == (5 * U, 10 * U)
    assert m.test_median_raw == 5 * U
    assert m.test_mean_raw == 7 * U  # (5+5+10+5+10)/5
    assert m.large_mean_raw == 27_600 * U


# 15. Dust protection
def test_dust_never_becomes_a_test():
    h = hist([("0.000001", 20_000), ("0.000001", 30_000), ("0.000001", 40_000)])
    assert detect_sequences(h, ratio=S.ratio_fraction, max_followup=timedelta(hours=168), dust_floor_raw=S.dust_floor_raw) == []
    assert analyze_history("S", "R", h, NOW, S) is None


# 16. Random small transfers are not tests
def test_random_small_transfers_rejected():
    h = hist([], extra=[(d, a) for d, a in zip(range(1, 10), (1, 5, 3, 7, 2, 9, 4, 6, 8))])
    assert analyze_history("S", "R", h, NOW, S) is None


def test_single_isolated_sequence_is_not_enough():
    m = model([(5, 20_000)])
    assert not m.qualifies_active and not m.qualifies_candidate


def test_random_amount_relationships_rarely_qualify():
    rng = random.Random(7)
    active = 0
    for _ in range(200):
        t, h = NOW - timedelta(days=30), []
        for i in range(rng.randint(8, 60)):
            t += timedelta(minutes=rng.expovariate(1 / 600))
            h.append(HistTx(i, "x", 0, t, int(10 ** rng.uniform(0, 5) * U)))
        m = analyze_history("S", "R", h, NOW, S)
        active += bool(m and m.qualifies_active)
    assert active <= 2  # <=1 %: consistency / success-rate gates reject random flows


def test_small_transfers_without_followups_lower_success_rate():
    base = [(5, 20_000), (5, 30_000), (10, 40_000)]
    clean = model(base)
    noisy = model(base, extra=[(d, 5) for d in (20, 18, 16, 14, 12, 10, 9, 8)])  # 8 unanswered "tests"
    assert noisy.success_rate < 0.5 < clean.success_rate
    assert not noisy.qualifies_active


# 24. Pattern model updating
def test_model_updates_with_new_sequence():
    m3 = model([(5, 20_000), (5, 30_000), (10, 40_000)])
    m4 = model([(5, 20_000), (5, 30_000), (10, 40_000), (6, 60_000)])
    assert (m3.successful_sequences, m4.successful_sequences) == (3, 4)
    assert m4.large_max_raw == 60_000 * U and m4.score >= m3.score - 0.05
    assert m4.large_mean_raw > m3.large_mean_raw


# 25. Pattern decay
def test_pattern_decay_old_behaviour_is_replaced():
    old = [(5, 20_000), (5, 30_000), (10, 40_000)]
    new = [(5000, 100_000), (5000, 110_000), (6000, 120_000)]
    h = hist(old, start_days=100) + [
        HistTx(100 + i, f"n{i}", 0, x.timestamp, x.amount_raw) for i, x in enumerate(hist(new, start_days=6))
    ]
    m = analyze_history("S", "R", h, NOW, S)
    assert m.test_min_raw == 5000 * U  # recent behaviour dominates
    assert m.match_low_raw > 10 * U  # old 5 USDT test no longer matches


def test_decay_single_changed_sequence_weakens():
    h = hist([(5, 20_000), (5, 30_000), (10, 40_000)], start_days=75) + [
        HistTx(99, "z", 0, NOW - timedelta(days=1), 5000 * U),
        HistTx(98, "y", 0, NOW - timedelta(days=1) + timedelta(minutes=10), 100_000 * U),
    ]
    m = analyze_history("S", "R", h, NOW, S)
    assert not m.qualifies_active


def test_expired_pattern():
    m = model([(5, 20_000), (5, 30_000), (10, 40_000)], start_days=110)
    assert m.expired and not m.qualifies_active


# 14. Relationship independence (engine level)
def test_models_are_independent():
    a = analyze_history("A", "B", hist([(5, 20_000), (5, 30_000), (10, 40_000)]), NOW, S)
    c = analyze_history("A", "C", hist([(500, 50_000), (500, 60_000), (450, 55_000)]), NOW, S)
    assert (a.test_min_raw, c.test_min_raw) == (5 * U, 450 * U)


# test detector + follow-up detector
def _snap(m, status=WatchlistStatus.ACTIVE):
    return WatchlistSnapshot(
        id=1, sender="S", recipient="R", status=status, confidence="HIGH", confidence_score=0.9, pattern_strength=3,
        match_low_raw=m.match_low_raw, match_high_raw=m.match_high_raw, test_min_raw=m.test_min_raw,
        test_max_raw=m.test_max_raw, typical_test_raw=m.test_mean_raw, median_test_raw=m.test_median_raw,
        typical_large_raw=m.large_mean_raw, median_large_raw=m.large_median_raw, large_min_raw=m.large_min_raw,
        large_max_raw=m.large_max_raw, ratio="4000", successful_sequences=3, success_rate=1.0,
        typical_followup_seconds=1200, followup_window_seconds=3600, followup_p80_seconds=1500,
    )


def test_test_detector_matches_learned_band_only():
    snap = _snap(model([(5, 20_000), (5, 30_000), (10, 40_000)]))
    assert match_test_transfer(snap, 5 * U, NOW).matched
    assert match_test_transfer(snap, 10 * U, NOW).matched
    assert not match_test_transfer(snap, 1, NOW).matched  # dust
    assert not match_test_transfer(snap, 1 * U, NOW).matched
    assert not match_test_transfer(snap, 500 * U, NOW).matched
    assert not match_test_transfer(snap, 20_000 * U, NOW).matched
    assert not match_test_transfer(_snap(model([(5, 20_000)] * 3), WatchlistStatus.CANDIDATE), 5 * U, NOW).matched


def test_followup_selection():
    class TE:
        def __init__(self, amt, ts, exp):
            self.amount_raw, self.tx_timestamp, self.expires_at = amt, ts, exp

    te = TE(5 * U, NOW, NOW + timedelta(hours=1))
    assert select_followup([te], 20_000 * U, NOW + timedelta(minutes=10), S.ratio_fraction) is te
    assert select_followup([te], 40 * U, NOW + timedelta(minutes=10), S.ratio_fraction) is None  # 8x < 10x
    assert select_followup([te], 20_000 * U, NOW + timedelta(hours=2), S.ratio_fraction) is None  # window over
