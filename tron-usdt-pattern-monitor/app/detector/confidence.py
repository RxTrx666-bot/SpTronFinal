"""Confidence scoring for a learned Sender -> Recipient TEST -> LARGE pattern.

All scores are in [0, 1].  They are computed from the relationship's *own*
history only - there is no global notion of what a "test amount" is.

Signals (spec section 18 / 28):
  count          - how many (recency-weighted) successful sequences exist
  test           - are the test amounts clustered?  (log-scale dispersion)
  large          - are the follow-up amounts consistent?
  ratio          - clear separation between the test cluster and the larges
  timing         - does the large usually follow quickly and consistently?
  success        - how often did a test-like transfer lead to a large one?
  relationship   - how much of this pair's activity the pattern explains
                   (the same recipient repeatedly receives test AND large)
  recency        - is the pattern still active?

Floats are fine here: these are statistics, never token amounts.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from app.domain import ConfidenceLevel

WEIGHTS = {
    "count": 0.20,
    "test": 0.15,
    "large": 0.10,
    "ratio": 0.10,
    "timing": 0.10,
    "success": 0.15,
    "relationship": 0.10,
    "recency": 0.10,
}

# Scale parameters (log-space standard deviations considered "consistent").
TEST_LOG_STD_SCALE = 0.7  # ~2x spread still scores well
LARGE_LOG_STD_SCALE = 1.2  # larges are allowed to vary more
TIMING_LOG_STD_SCALE = 1.5
RATIO_FULL_SCORE = 100.0  # separation of 100x (min large / max test) scores 1.0
PROMPT_SECONDS = 6 * 3600


@dataclass
class ConfidenceComponents:
    count: float = 0.0
    test: float = 0.0
    large: float = 0.0
    ratio: float = 0.0
    timing: float = 0.0
    success: float = 0.0
    relationship: float = 0.0
    recency: float = 0.0

    @property
    def score(self) -> float:
        return round(sum(getattr(self, k) * w for k, w in WEIGHTS.items()), 4)

    def as_dict(self) -> dict[str, float]:
        d = {k: round(v, 3) for k, v in asdict(self).items()}
        d["score"] = self.score
        return d


def clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def weighted_std(values: list[float], weights: list[float]) -> float:
    total = sum(weights)
    if not values or total <= 0:
        return 0.0
    mean = sum(v * w for v, w in zip(values, weights)) / total
    var = sum(w * (v - mean) ** 2 for v, w in zip(values, weights)) / total
    return math.sqrt(max(var, 0.0))


def consistency_score(log_values: list[float], weights: list[float], scale: float) -> float:
    std = weighted_std(log_values, weights)
    return clamp01(math.exp(-((std / scale) ** 2)))


def count_score(effective_sequences: float) -> float:
    return clamp01(1.0 - math.exp(-effective_sequences / 2.0))


def ratio_score(min_large_raw: int, max_test_raw: int) -> float:
    if max_test_raw <= 0 or min_large_raw <= max_test_raw:
        return 0.0
    return clamp01(math.log(min_large_raw / max_test_raw) / math.log(RATIO_FULL_SCORE))


def timing_score(dts: list[int], weights: list[float], max_followup_seconds: float) -> float:
    if not dts:
        return 0.0
    logs = [math.log(dt + 60) for dt in dts]
    consistency = consistency_score(logs, weights, TIMING_LOG_STD_SCALE)
    med = sorted(dts)[len(dts) // 2]
    if med <= PROMPT_SECONDS or max_followup_seconds <= PROMPT_SECONDS:
        promptness = 1.0
    else:
        frac = (med - PROMPT_SECONDS) / (max_followup_seconds - PROMPT_SECONDS)
        promptness = max(0.3, 1.0 - 0.7 * clamp01(frac))
    return clamp01(0.6 * consistency + 0.4 * promptness)


def recency_score(days_since_last: float, half_life_days: float) -> float:
    if half_life_days <= 0:
        return 1.0
    return clamp01(0.5 ** (max(days_since_last, 0.0) / half_life_days))


def level_for(score: float, high: float, medium: float) -> ConfidenceLevel:
    if score >= high:
        return ConfidenceLevel.HIGH
    if score >= medium:
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.LOW
