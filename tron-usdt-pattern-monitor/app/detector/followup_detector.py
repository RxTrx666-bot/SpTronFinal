"""Large follow-up detection after a known test transfer was alerted.

A follow-up is a transfer of the SAME Sender -> Recipient pair, after the test
and inside the relationship's learned follow-up window (stored on the pending
test event as ``expires_at``), that is at least MIN_LARGE_TO_TEST_RATIO times
larger than the test transfer.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from fractions import Fraction
from typing import Protocol

from app.detector.pattern_engine import is_substantially_larger


class PendingTest(Protocol):
    amount_raw: int
    tx_timestamp: datetime
    expires_at: datetime


def is_large_followup(test_amount_raw: int, amount_raw: int, ratio: Fraction, min_large_raw: int = 0) -> bool:
    return amount_raw >= min_large_raw and is_substantially_larger(amount_raw, test_amount_raw, ratio)


def select_followup(
    pending: Sequence[PendingTest], amount_raw: int, at: datetime, ratio: Fraction, min_large_raw: int = 0
):
    """Return the most recent pending test this transfer completes, or None."""
    for te in sorted(pending, key=lambda t: t.tx_timestamp, reverse=True):
        if te.tx_timestamp <= at <= te.expires_at and is_large_followup(te.amount_raw, amount_raw, ratio, min_large_raw):
            return te
    return None
