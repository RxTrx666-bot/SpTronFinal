"""Watchlist test-transfer matching.

A new transfer is a *known test* only when:
  * its Sender -> Recipient pair is on the automatic watchlist with status
    ACTIVE (optionally WEAKENED, see ALERT_ON_WEAKENED), and
  * its amount falls inside the band learned for THAT relationship.

There is no global amount threshold anywhere in this decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.domain import WatchlistStatus
from app.watchlist.model import WatchlistSnapshot


@dataclass(frozen=True)
class TestMatch:
    __test__ = False  # not a pytest test class

    matched: bool
    reason: str


def match_test_transfer(
    snap: WatchlistSnapshot | None,
    amount_raw: int,
    now: datetime,
    *,
    alert_on_weakened: bool = False,
) -> TestMatch:
    if snap is None:
        return TestMatch(False, "pair_not_on_watchlist")
    if snap.status == WatchlistStatus.PAUSED:
        if snap.manual_pause or snap.pause_until is None or snap.pause_until > now:
            return TestMatch(False, "paused")
    elif snap.status == WatchlistStatus.WEAKENED:
        if not alert_on_weakened:
            return TestMatch(False, "weakened")
    elif snap.status != WatchlistStatus.ACTIVE:
        return TestMatch(False, f"status_{snap.status.value.lower()}")
    if amount_raw < snap.match_low_raw:
        return TestMatch(False, "below_learned_test_range")
    if amount_raw > snap.match_high_raw:
        return TestMatch(False, "above_learned_test_range")
    return TestMatch(True, "matches_learned_test_behaviour")
