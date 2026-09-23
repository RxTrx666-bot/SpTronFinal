"""In-memory view of the automatic watchlist.

The watchlist itself lives in PostgreSQL; this cache mirrors it so that the hot
path (every incoming USDT transfer) can check "is this Sender -> Recipient on
the watchlist?" with a dict lookup instead of a database query.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from app.database.models import WatchlistEntry
from app.domain import WatchlistStatus


@dataclass(frozen=True)
class WatchlistSnapshot:
    id: int
    sender: str
    recipient: str
    status: WatchlistStatus
    confidence: str
    confidence_score: float
    pattern_strength: float
    match_low_raw: int
    match_high_raw: int
    test_min_raw: int
    test_max_raw: int
    typical_test_raw: int
    median_test_raw: int
    typical_large_raw: int
    median_large_raw: int
    large_min_raw: int
    large_max_raw: int
    ratio: str
    successful_sequences: int
    success_rate: float
    typical_followup_seconds: int
    followup_window_seconds: int
    followup_p80_seconds: int
    recent_large_amounts: tuple[int, ...] = field(default_factory=tuple)
    pause_until: datetime | None = None
    manual_pause: bool = False
    last_sequence_at: datetime | None = None

    @property
    def pair(self) -> tuple[str, str]:
        return (self.sender, self.recipient)

    @classmethod
    def from_entry(cls, e: WatchlistEntry) -> "WatchlistSnapshot":
        extra = json.loads(e.model_json) if e.model_json else {}
        return cls(
            id=e.id,
            sender=e.sender,
            recipient=e.recipient,
            status=WatchlistStatus(e.status),
            confidence=e.confidence,
            confidence_score=e.confidence_score,
            pattern_strength=e.pattern_strength,
            match_low_raw=int(e.match_low_raw),
            match_high_raw=int(e.match_high_raw),
            test_min_raw=int(e.test_amount_min_raw),
            test_max_raw=int(e.test_amount_max_raw),
            typical_test_raw=int(e.typical_test_amount_raw),
            median_test_raw=int(e.median_test_amount_raw),
            typical_large_raw=int(e.typical_large_amount_raw),
            median_large_raw=int(e.median_large_amount_raw),
            large_min_raw=int(e.large_amount_min_raw),
            large_max_raw=int(e.large_amount_max_raw),
            ratio=str(e.typical_test_to_large_ratio),
            successful_sequences=e.successful_sequences,
            success_rate=e.success_rate,
            typical_followup_seconds=int(e.typical_followup_seconds),
            followup_window_seconds=int(e.followup_window_seconds),
            followup_p80_seconds=int(extra.get("followup_p80_seconds", e.typical_followup_seconds)),
            recent_large_amounts=tuple(int(x) for x in extra.get("recent_large_amounts", [])),
            pause_until=e.pause_until,
            manual_pause=e.manual_pause,
            last_sequence_at=e.last_sequence_at,
        )


class WatchlistCache:
    def __init__(self) -> None:
        self._by_pair: dict[tuple[str, str], WatchlistSnapshot] = {}

    def load(self, entries: list[WatchlistEntry]) -> None:
        self._by_pair = {(e.sender, e.recipient): WatchlistSnapshot.from_entry(e) for e in entries}

    def put(self, snap: WatchlistSnapshot) -> None:
        self._by_pair[snap.pair] = snap

    def get(self, sender: str, recipient: str) -> WatchlistSnapshot | None:
        return self._by_pair.get((sender, recipient))

    def pairs(self) -> set[tuple[str, str]]:
        return set(self._by_pair)

    def __contains__(self, pair: tuple[str, str]) -> bool:
        return pair in self._by_pair

    def __len__(self) -> int:
        return len(self._by_pair)

    def values(self) -> list[WatchlistSnapshot]:
        return list(self._by_pair.values())
