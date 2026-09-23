"""Behavioural pattern engine (pure logic, no I/O).

Unit of analysis: ONE Sender -> Recipient relationship.  The engine receives
that relationship's own transfer history and:

1. ``detect_sequences`` - finds TEST -> LARGE sequences: a transfer followed,
   within MAX_FOLLOWUP_HOURS, by a transfer of the same pair that is at least
   MIN_LARGE_TO_TEST_RATIO times larger.  Nothing here says what a test amount
   *is*; a test is only defined relative to the transfer that follows it.

2. ``build_model`` - learns the relationship's specific test behaviour:
   * clusters the test amounts in log space (relative band around the
     relationship's own centre, recency weighted) - so 5-10 USDT, 200-300 USDT
     and 950-1,100 USDT are all learned independently;
   * derives the learned test range / typical amounts / follow-up window;
   * measures success rate (how often a test-like transfer actually led to a
     large one), consistency, timing and recency;
   * produces a confidence score and whether the relationship qualifies for
     the automatic watchlist.

Dust / random small transfers never qualify on their own: qualification needs
repeated, consistent, successful sequences (historical behavioural evidence).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from fractions import Fraction
from typing import TYPE_CHECKING

from app.detector import confidence as conf
from app.domain import ConfidenceLevel

if TYPE_CHECKING:  # pragma: no cover
    from app.config.settings import Settings


@dataclass(frozen=True)
class HistTx:
    id: int
    tx_hash: str
    event_index: int
    timestamp: datetime
    amount_raw: int


@dataclass(frozen=True)
class DetectedSequence:
    test: HistTx
    large: HistTx

    @property
    def dt_seconds(self) -> int:
        return max(0, int((self.large.timestamp - self.test.timestamp).total_seconds()))

    @property
    def ratio(self) -> Fraction:
        return Fraction(self.large.amount_raw, self.test.amount_raw)


@dataclass
class PatternModel:
    sender: str
    recipient: str
    sequences: list[DetectedSequence]
    inliers: list[DetectedSequence]
    effective_sequences: float
    inlier_fraction: float
    test_min_raw: int
    test_max_raw: int
    test_mean_raw: int
    test_median_raw: int
    large_min_raw: int
    large_max_raw: int
    large_mean_raw: int
    large_median_raw: int
    ratio_median: Fraction
    followup_median_seconds: int
    followup_p80_seconds: int
    followup_max_seconds: int
    followup_window_seconds: int
    match_low_raw: int
    match_high_raw: int
    success_rate: float
    judged_tests: float
    successful_tests: float
    relationship_consistency: float
    last_sequence_at: datetime
    last_test_at: datetime
    last_large_at: datetime
    components: conf.ConfidenceComponents
    confidence: ConfidenceLevel
    pattern_strength: float
    qualifies_active: bool
    qualifies_candidate: bool
    expired: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        return self.components.score

    @property
    def successful_sequences(self) -> int:
        return len(self.inliers)

    def recent_large_amounts(self, n: int = 5) -> list[int]:
        return [s.large.amount_raw for s in sorted(self.inliers, key=lambda s: s.large.timestamp)][-n:]

    def recent_test_amounts(self, n: int = 10) -> list[int]:
        return [s.test.amount_raw for s in sorted(self.inliers, key=lambda s: s.test.timestamp)][-n:]


# --------------------------------------------------------------------------
# Sequence detection
# --------------------------------------------------------------------------


def is_substantially_larger(large_raw: int, test_raw: int, ratio: Fraction) -> bool:
    """Exact rational comparison: large / test >= ratio."""
    return test_raw > 0 and large_raw * ratio.denominator >= test_raw * ratio.numerator


def detect_sequences(
    history: list[HistTx],
    *,
    ratio: Fraction,
    max_followup: timedelta,
    dust_floor_raw: int = 0,
) -> list[DetectedSequence]:
    """Pair each transfer with the most recent unused earlier transfer of the same
    relationship that it exceeds by ``ratio`` within ``max_followup``.

    A transfer used as the LARGE side can never be a TEST, and each test pairs at
    most once, so one test followed by several large tranches yields one sequence.
    """
    txs = sorted(history, key=lambda t: (t.timestamp, t.id))
    used_as_test: set[int] = set()
    used_as_large: set[int] = set()
    out: list[DetectedSequence] = []
    for j, x in enumerate(txs):
        for i in range(j - 1, -1, -1):
            t = txs[i]
            if x.timestamp - t.timestamp > max_followup:
                break
            if i in used_as_test or i in used_as_large:
                continue
            if t.amount_raw < max(dust_floor_raw, 1):
                continue
            if is_substantially_larger(x.amount_raw, t.amount_raw, ratio):
                used_as_test.add(i)
                used_as_large.add(j)
                out.append(DetectedSequence(test=t, large=x))
                break
    return out


# --------------------------------------------------------------------------
# Helpers (exact integer statistics for amounts)
# --------------------------------------------------------------------------


def median_int(values: list[int]) -> int:
    s = sorted(values)
    n = len(s)
    if n == 0:
        return 0
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) // 2


def mean_int(values: list[int]) -> int:
    return sum(values) // len(values) if values else 0


def percentile_int(values: list[int], q: float) -> int:
    s = sorted(values)
    if not s:
        return 0
    idx = min(len(s) - 1, max(0, math.ceil(q * len(s)) - 1))
    return s[idx]


def _weighted_median(values: list[float], weights: list[float]) -> float:
    pairs = sorted(zip(values, weights))
    total = sum(weights)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= total / 2:
            return v
    return pairs[-1][0]


def _decay(ts: datetime, now: datetime, half_life_days: float) -> float:
    if half_life_days <= 0:
        return 1.0
    age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
    return 0.5 ** (age_days / half_life_days)


def _fraction(x: float) -> Fraction:
    return Fraction(str(x))


# --------------------------------------------------------------------------
# Model building
# --------------------------------------------------------------------------


def select_test_cluster(
    sequences: list[DetectedSequence], weights: list[float], band_factor: float
) -> list[int]:
    """Indices of the sequences forming the dominant (recency-weighted) test cluster."""
    if not sequences:
        return []
    logs = [math.log(s.test.amount_raw) for s in sequences]
    band = math.log(band_factor) + 1e-9
    best_key = None
    best_members: list[int] = []
    for i in range(len(sequences)):
        members = [k for k in range(len(sequences)) if abs(logs[k] - logs[i]) <= band]
        key = (
            round(sum(weights[k] for k in members), 9),
            len(members),
            max(sequences[k].large.timestamp for k in members),
        )
        if best_key is None or key > best_key:
            best_key, best_members = key, members
    # Refine around the weighted median of the chosen cluster.
    centre = _weighted_median([logs[k] for k in best_members], [weights[k] for k in best_members])
    return [k for k in range(len(sequences)) if abs(logs[k] - centre) <= band]


def build_model(
    sender: str,
    recipient: str,
    history: list[HistTx],
    sequences: list[DetectedSequence],
    now: datetime,
    settings: "Settings",
) -> PatternModel | None:
    if not sequences:
        return None
    ratio = settings.ratio_fraction
    half_life = settings.pattern_half_life_days
    max_followup_s = settings.max_followup_hours * 3600

    weights = [_decay(s.large.timestamp, now, half_life) for s in sequences]
    idx = select_test_cluster(sequences, weights, settings.test_cluster_band_factor)
    inliers = [sequences[k] for k in idx]
    in_w = [weights[k] for k in idx]
    total_w = sum(weights)
    eff = sum(in_w)
    inlier_fraction = eff / total_w if total_w > 0 else 0.0

    tests = [s.test.amount_raw for s in inliers]
    larges = [s.large.amount_raw for s in inliers]
    dts = [s.dt_seconds for s in inliers]
    ratios = sorted(s.ratio for s in inliers)
    test_min, test_max = min(tests), max(tests)
    large_min, large_max = min(larges), max(larges)

    # ---- learned follow-up window (per relationship) ------------------------
    median_dt = median_int(dts)
    window = max(
        max(dts) * 2,
        int(median_dt * settings.followup_window_multiplier),
        settings.min_followup_window_minutes * 60,
    )
    window = int(min(window, max_followup_s))

    # ---- learned test band used for live matching (relative) ----------------
    margin = _fraction(settings.test_match_margin)
    low = math.floor(Fraction(test_min) * (1 - margin)) if margin < 1 else 0
    low = max(low, settings.dust_floor_raw, 1)
    high = math.floor(Fraction(test_max) * (1 + margin))
    high = min(high, math.floor(Fraction(large_min) / ratio))

    # ---- success rate & relationship consistency ----------------------------
    large_ids = {s.large.id for s in sequences}
    test_ids = {s.test.id for s in sequences}
    inlier_large_ids = {s.large.id for s in inliers}
    succ = judged = considered = explained = 0.0
    for tx in history:
        w = _decay(tx.timestamp, now, half_life)
        if tx.id in large_ids:
            considered += w
            if tx.id in inlier_large_ids:
                explained += w
            continue
        in_band = low <= tx.amount_raw <= high
        if in_band:
            if tx.id in test_ids:
                succ += w
                judged += w
            elif (now - tx.timestamp).total_seconds() > window:
                judged += w
            else:
                continue  # still inside its follow-up window: not judged yet
            explained += w
        considered += w
    success_rate = succ / judged if judged > 0 else 0.0
    relationship = explained / considered if considered > 0 else 0.0

    last_seq = max(s.large.timestamp for s in inliers)
    days_since = max(0.0, (now - last_seq).total_seconds() / 86400.0)

    comps = conf.ConfidenceComponents(
        count=conf.count_score(eff),
        test=conf.consistency_score(
            [math.log(s.test.amount_raw) for s in sequences], weights, conf.TEST_LOG_STD_SCALE
        ),
        large=conf.consistency_score([math.log(v) for v in larges], in_w, conf.LARGE_LOG_STD_SCALE),
        ratio=conf.ratio_score(large_min, test_max),
        timing=conf.timing_score(dts, in_w, max_followup_s),
        success=conf.clamp01(success_rate),
        relationship=conf.clamp01(relationship),
        recency=conf.recency_score(days_since, half_life),
    )
    level = conf.level_for(comps.score, settings.confidence_high_threshold, settings.confidence_medium_threshold)
    expired = days_since > settings.pattern_expiry_days

    reasons: list[str] = []
    if len(inliers) < settings.min_successful_sequences:
        reasons.append(f"only {len(inliers)}/{settings.min_successful_sequences} consistent sequences")
    if level.rank < settings.min_pattern_confidence.rank:
        reasons.append(f"confidence {level.value} below {settings.min_pattern_confidence.value}")
    if success_rate < settings.min_success_rate:
        reasons.append(f"success rate {success_rate:.0%} below {settings.min_success_rate:.0%}")
    if inlier_fraction < settings.min_inlier_fraction:
        reasons.append(f"test amounts inconsistent ({inlier_fraction:.0%} in dominant cluster)")
    if expired:
        reasons.append(f"no sequence for {days_since:.0f} days")
    if high < low:
        reasons.append("test band overlaps large band")

    qualifies_active = not reasons
    qualifies_candidate = (
        len(inliers) >= settings.candidate_min_sequences
        and not expired
        and high >= low
        and inlier_fraction >= settings.min_inlier_fraction
    )

    return PatternModel(
        sender=sender,
        recipient=recipient,
        sequences=sequences,
        inliers=inliers,
        effective_sequences=round(eff, 4),
        inlier_fraction=round(inlier_fraction, 4),
        test_min_raw=test_min,
        test_max_raw=test_max,
        test_mean_raw=mean_int(tests),
        test_median_raw=median_int(tests),
        large_min_raw=large_min,
        large_max_raw=large_max,
        large_mean_raw=mean_int(larges),
        large_median_raw=median_int(larges),
        ratio_median=ratios[len(ratios) // 2],
        followup_median_seconds=median_dt,
        followup_p80_seconds=percentile_int(dts, 0.8),
        followup_max_seconds=max(dts),
        followup_window_seconds=window,
        match_low_raw=low,
        match_high_raw=high,
        success_rate=round(success_rate, 4),
        judged_tests=round(judged, 4),
        successful_tests=round(succ, 4),
        relationship_consistency=round(relationship, 4),
        last_sequence_at=last_seq,
        last_test_at=max(s.test.timestamp for s in inliers),
        last_large_at=last_seq,
        components=comps,
        confidence=level,
        pattern_strength=round(eff * success_rate, 3),
        qualifies_active=qualifies_active,
        qualifies_candidate=qualifies_candidate,
        expired=expired,
        reasons=reasons,
    )


def analyze_history(
    sender: str, recipient: str, history: list[HistTx], now: datetime, settings: "Settings"
) -> PatternModel | None:
    """Convenience: detect sequences and build the model in one call."""
    seqs = detect_sequences(
        history,
        ratio=settings.ratio_fraction,
        max_followup=timedelta(hours=settings.max_followup_hours),
        dust_floor_raw=settings.dust_floor_raw,
    )
    return build_model(sender, recipient, history, seqs, now, settings)
