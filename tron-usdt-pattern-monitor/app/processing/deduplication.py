"""Duplicate protection.

Two layers:
1. ``SeenCache`` - an in-memory LRU that drops events we already processed in
   this process (the collector re-reads a small overlap every poll), saving
   database round-trips.  It is an optimisation only.
2. The database - the authoritative guard.  Unique constraints on
   ``transactions(transaction_hash, event_index)``, ``test_events``,
   ``followup_events`` and ``alerts(dedup_key)`` combined with
   ``INSERT ... ON CONFLICT DO NOTHING`` guarantee that a transfer is stored
   once and produces at most one alert of each type - across API retries,
   duplicate deliveries, process crashes and VPS restarts.
"""

from __future__ import annotations

from collections import OrderedDict

from app.domain import AlertType, TransferEvent


class SeenCache:
    def __init__(self, capacity: int = 200_000) -> None:
        self.capacity = capacity
        self._data: OrderedDict[tuple[str, int, bool], None] = OrderedDict()

    def _key(self, ev: TransferEvent) -> tuple[str, int, bool]:
        return (ev.transaction_hash, ev.event_index, ev.confirmed)

    def filter_new(self, events: list[TransferEvent]) -> list[TransferEvent]:
        out = []
        batch: set[tuple[str, int, bool]] = set()
        for ev in events:
            k = self._key(ev)
            if k in self._data or k in batch:
                continue
            batch.add(k)
            out.append(ev)
        return out

    def mark(self, events: list[TransferEvent]) -> None:
        for ev in events:
            k = self._key(ev)
            self._data[k] = None
            self._data.move_to_end(k)
        while len(self._data) > self.capacity:
            self._data.popitem(last=False)

    def __len__(self) -> int:
        return len(self._data)


def alert_dedup_key(alert_type: AlertType, tx_hash: str, event_index: int) -> str:
    """One alert of a given type per on-chain transfer, ever."""
    return f"{alert_type.value}:{tx_hash}:{event_index}"
