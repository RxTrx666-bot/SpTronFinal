"""Data-access functions.  All writes that must be idempotent use
``INSERT ... ON CONFLICT DO NOTHING`` against unique constraints, so duplicate
API deliveries, retries and restarts can never create duplicate transactions,
test events, follow-ups or alerts.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, text, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.amounts import raw_to_usdt
from app.database.models import (
    Alert,
    CollectorState,
    FollowupEvent,
    PatternSequence,
    TestEvent,
    Transaction,
    WalletPair,
    WatchlistEntry,
)
from app.detector.pattern_engine import HistTx, PatternModel, _ratio_to_decimal
from app.domain import AlertStatus, TestEventStatus, TransferEvent, TxStatus


def _insert(session: AsyncSession, table):
    if session.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert(table)


def _least(session: AsyncSession, a, b):
    return func.least(a, b) if session.bind.dialect.name == "postgresql" else func.min(a, b)


def _greatest(session: AsyncSession, a, b):
    return func.greatest(a, b) if session.bind.dialect.name == "postgresql" else func.max(a, b)


# ---------------------------------------------------------------- transactions


@dataclass
class StoredTx:
    id: int
    transaction_hash: str
    event_index: int
    sender: str
    recipient: str
    amount_raw: int
    timestamp: datetime
    status: str
    detected_at: datetime
    newly_confirmed: bool = False
    upgraded: bool = False

    @property
    def pair(self) -> tuple[str, str]:
        return (self.sender, self.recipient)


def _stored(row: Any, **kw) -> StoredTx:
    return StoredTx(
        id=row.id,
        transaction_hash=row.transaction_hash,
        event_index=row.event_index,
        sender=row.sender,
        recipient=row.recipient,
        amount_raw=int(row.amount_raw),
        timestamp=row.timestamp,
        status=row.status,
        detected_at=row.detected_at,
        **kw,
    )


_TX_COLS = (
    Transaction.id,
    Transaction.transaction_hash,
    Transaction.event_index,
    Transaction.sender,
    Transaction.recipient,
    Transaction.amount_raw,
    Transaction.timestamp,
    Transaction.status,
    Transaction.detected_at,
)


async def insert_transactions(
    session: AsyncSession,
    events: Sequence[TransferEvent],
    *,
    detected_at: datetime,
    needs_matching: set[tuple[str, str]],
    decimals: int,
) -> list[StoredTx]:
    """Insert new transfers; duplicates are silently ignored. Returns inserted rows."""
    if not events:
        return []
    rows = []
    seen: set[tuple[str, int]] = set()
    for ev in events:
        if ev.key in seen:
            continue
        seen.add(ev.key)
        rows.append(
            dict(
                transaction_hash=ev.transaction_hash,
                event_index=ev.event_index,
                block_number=ev.block_number,
                timestamp=ev.timestamp,
                sender=ev.sender,
                recipient=ev.recipient,
                amount_raw=ev.amount_raw,
                amount_usdt=raw_to_usdt(ev.amount_raw, decimals),
                token_contract=ev.token_contract,
                status=TxStatus.CONFIRMED.value if ev.confirmed else TxStatus.UNCONFIRMED.value,
                detected_at=detected_at,
                confirmed_at=detected_at if ev.confirmed else None,
                processed=ev.pair not in needs_matching,
            )
        )
    stmt = (
        _insert(session, Transaction)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["transaction_hash", "event_index"])
        .returning(*_TX_COLS)
    )
    res = await session.execute(stmt)
    return [_stored(r, newly_confirmed=r.status == TxStatus.CONFIRMED.value) for r in res]


async def confirm_transactions(
    session: AsyncSession,
    events: Sequence[TransferEvent],
    *,
    now: datetime,
    rematch: set[tuple[str, str]],
) -> list[StoredTx]:
    """Upgrade UNCONFIRMED rows that have now been seen as confirmed."""
    keys = list({ev.key for ev in events if ev.confirmed})
    if not keys:
        return []
    out: list[StoredTx] = []
    for i in range(0, len(keys), 500):
        chunk = keys[i : i + 500]
        stmt = (
            update(Transaction)
            .where(
                tuple_(Transaction.transaction_hash, Transaction.event_index).in_(chunk),
                Transaction.status == TxStatus.UNCONFIRMED.value,
            )
            .values(status=TxStatus.CONFIRMED.value, confirmed_at=now)
            .returning(*_TX_COLS)
        )
        res = await session.execute(stmt)
        out.extend(_stored(r, newly_confirmed=True, upgraded=True) for r in res)
    remark = [t.id for t in out if t.pair in rematch]
    if remark:
        await session.execute(update(Transaction).where(Transaction.id.in_(remark)).values(processed=False))
    return out


async def get_transaction(session: AsyncSession, tx_id: int) -> StoredTx | None:
    row = (await session.execute(select(*_TX_COLS).where(Transaction.id == tx_id))).first()
    return _stored(row) if row else None


async def mark_processed(session: AsyncSession, tx_id: int) -> None:
    await session.execute(update(Transaction).where(Transaction.id == tx_id).values(processed=True))


async def unprocessed_transactions(session: AsyncSession, before: datetime, limit: int = 500) -> list[StoredTx]:
    rows = await session.execute(
        select(*_TX_COLS)
        .where(Transaction.processed.is_(False), Transaction.detected_at <= before)
        .order_by(Transaction.id)
        .limit(limit)
    )
    return [_stored(r) for r in rows]


async def pair_history(
    session: AsyncSession, sender: str, recipient: str, since: datetime, limit: int
) -> list[HistTx]:
    rows = await session.execute(
        select(
            Transaction.id,
            Transaction.transaction_hash,
            Transaction.event_index,
            Transaction.timestamp,
            Transaction.amount_raw,
        )
        .where(
            Transaction.sender == sender,
            Transaction.recipient == recipient,
            Transaction.timestamp >= since,
            Transaction.status == TxStatus.CONFIRMED.value,
        )
        .order_by(Transaction.timestamp.desc(), Transaction.id.desc())
        .limit(limit)
    )
    out = [HistTx(r.id, r.transaction_hash, r.event_index, r.timestamp, int(r.amount_raw)) for r in rows]
    out.reverse()
    return out


async def recent_pair_transfers(session: AsyncSession, sender: str, recipient: str, limit: int = 10):
    rows = await session.execute(
        select(Transaction)
        .where(Transaction.sender == sender, Transaction.recipient == recipient)
        .order_by(Transaction.timestamp.desc())
        .limit(limit)
    )
    return list(rows.scalars())


async def mark_dropped_unconfirmed(session: AsyncSession, older_than: datetime) -> list[int]:
    res = await session.execute(
        update(Transaction)
        .where(Transaction.status == TxStatus.UNCONFIRMED.value, Transaction.timestamp < older_than)
        .values(status=TxStatus.DROPPED.value, processed=True)
        .returning(Transaction.id)
    )
    return [r.id for r in res]


async def prune_transactions(session: AsyncSession, older_than: datetime, batch: int = 5000) -> int:
    ids = select(Transaction.id).where(Transaction.timestamp < older_than).limit(batch).scalar_subquery()
    res = await session.execute(delete(Transaction).where(Transaction.id.in_(ids)))
    return res.rowcount or 0


# ---------------------------------------------------------------- wallet pairs


@dataclass
class PairAggregate:
    sender: str
    recipient: str
    count: int
    volume: int
    smallest: int
    largest: int
    first: datetime
    last: datetime


def aggregate_pairs(txs: Iterable[StoredTx]) -> list[PairAggregate]:
    agg: dict[tuple[str, str], PairAggregate] = {}
    for t in txs:
        a = agg.get(t.pair)
        if a is None:
            agg[t.pair] = PairAggregate(t.sender, t.recipient, 1, t.amount_raw, t.amount_raw, t.amount_raw, t.timestamp, t.timestamp)
        else:
            a.count += 1
            a.volume += t.amount_raw
            a.smallest = min(a.smallest, t.amount_raw)
            a.largest = max(a.largest, t.amount_raw)
            a.first = min(a.first, t.timestamp)
            a.last = max(a.last, t.timestamp)
    return list(agg.values())


async def upsert_pair_stats(
    session: AsyncSession,
    aggregates: Sequence[PairAggregate],
    *,
    now: datetime,
    ratio: Fraction,
    flag_analysis: bool,
) -> list[tuple[str, str]]:
    """Atomically fold new confirmed transfers into per-pair statistics.

    Returns the pairs flagged for (re-)analysis: a pair is flagged when it has
    more than one transfer and its largest amount is >= ratio x its smallest,
    i.e. a TEST -> LARGE sequence is *possible*.  This cheap prefilter keeps the
    heavy per-pair analysis off pairs that cannot contain a pattern.
    """
    if not aggregates:
        return []
    num, den = ratio.numerator, ratio.denominator
    flagged: list[tuple[str, str]] = []
    for i in range(0, len(aggregates), 500):
        chunk = sorted(aggregates[i : i + 500], key=lambda a: (a.sender, a.recipient))
        rows = [
            dict(
                sender=a.sender,
                recipient=a.recipient,
                total_transfers=a.count,
                total_volume_raw=a.volume,
                average_amount_raw=a.volume // a.count,
                median_amount_raw=None,
                smallest_amount_raw=a.smallest,
                largest_amount_raw=a.largest,
                first_seen_at=a.first,
                last_seen_at=a.last,
                sequence_count=0,
                needs_analysis=flag_analysis and a.count >= 2 and a.largest * den >= a.smallest * num,
                created_at=now,
                updated_at=now,
            )
            for a in chunk
        ]
        ins = _insert(session, WalletPair).values(rows)
        ex = ins.excluded
        wp = WalletPair.__table__.c
        new_total = wp.total_transfers + ex.total_transfers
        new_volume = wp.total_volume_raw + ex.total_volume_raw
        new_small = _least(session, wp.smallest_amount_raw, ex.smallest_amount_raw)
        set_ = {
            "total_transfers": new_total,
            "total_volume_raw": new_volume,
            "average_amount_raw": new_volume / new_total,
            "smallest_amount_raw": new_small,
            "largest_amount_raw": _greatest(session, wp.largest_amount_raw, ex.largest_amount_raw),
            "first_seen_at": _least(session, wp.first_seen_at, ex.first_seen_at),
            "last_seen_at": _greatest(session, wp.last_seen_at, ex.last_seen_at),
            "updated_at": ex.updated_at,
        }
        if flag_analysis:
            set_["needs_analysis"] = or_(
                wp.needs_analysis,
                ex.largest_amount_raw * den >= new_small * num,
            )
        stmt = ins.on_conflict_do_update(index_elements=["sender", "recipient"], set_=set_).returning(
            WalletPair.sender, WalletPair.recipient, WalletPair.needs_analysis
        )
        res = await session.execute(stmt)
        flagged.extend((r.sender, r.recipient) for r in res if r.needs_analysis)
    return flagged


async def update_pair_after_analysis(
    session: AsyncSession, sender: str, recipient: str, *, median_raw: int | None, sequence_count: int, now: datetime
) -> None:
    await session.execute(
        update(WalletPair)
        .where(WalletPair.sender == sender, WalletPair.recipient == recipient)
        .values(
            median_amount_raw=median_raw,
            sequence_count=sequence_count,
            needs_analysis=False,
            last_analyzed_at=now,
        )
    )


async def flagged_pairs(session: AsyncSession, limit: int = 500, after_id: int = 0) -> list[tuple[int, str, str]]:
    rows = await session.execute(
        select(WalletPair.id, WalletPair.sender, WalletPair.recipient)
        .where(WalletPair.needs_analysis.is_(True), WalletPair.id > after_id)
        .order_by(WalletPair.id)
        .limit(limit)
    )
    return [(r.id, r.sender, r.recipient) for r in rows]


async def flag_backfill_candidates(session: AsyncSession, *, min_transfers: int, ratio: Fraction) -> int:
    """After a historical backfill, flag every pair that could contain a pattern."""
    res = await session.execute(
        update(WalletPair)
        .where(
            WalletPair.total_transfers >= min_transfers,
            WalletPair.largest_amount_raw * ratio.denominator >= WalletPair.smallest_amount_raw * ratio.numerator,
        )
        .values(needs_analysis=True)
    )
    return res.rowcount or 0


async def get_pair(session: AsyncSession, sender: str, recipient: str) -> WalletPair | None:
    return (
        await session.execute(
            select(WalletPair).where(WalletPair.sender == sender, WalletPair.recipient == recipient)
        )
    ).scalar_one_or_none()


# ---------------------------------------------------------------- sequences


async def replace_sequences(
    session: AsyncSession, sender: str, recipient: str, since: datetime, model: PatternModel | None, now: datetime
) -> None:
    """Replace the pair's sequences inside the analysis window with the freshly
    detected set (analysis is deterministic, so this is idempotent)."""
    await session.execute(
        delete(PatternSequence).where(
            PatternSequence.sender == sender,
            PatternSequence.recipient == recipient,
            PatternSequence.test_timestamp >= since,
        )
    )
    if model is None or not model.sequences:
        return
    await session.flush()
    inlier_ids = {s.test.id for s in model.inliers}
    rows = [
        dict(
            sender=sender,
            recipient=recipient,
            test_transaction_id=s.test.id,
            large_transaction_id=s.large.id,
            test_tx_hash=s.test.tx_hash,
            large_tx_hash=s.large.tx_hash,
            test_amount_raw=s.test.amount_raw,
            large_amount_raw=s.large.amount_raw,
            amount_ratio=_ratio_to_decimal(s.ratio),
            test_timestamp=s.test.timestamp,
            large_timestamp=s.large.timestamp,
            time_difference_seconds=s.dt_seconds,
            is_inlier=s.test.id in inlier_ids,
            created_at=now,
        )
        for s in model.sequences
    ]
    stmt = _insert(session, PatternSequence).values(rows).on_conflict_do_nothing()
    await session.execute(stmt)


async def pair_sequences(session: AsyncSession, sender: str, recipient: str, limit: int = 20):
    rows = await session.execute(
        select(PatternSequence)
        .where(PatternSequence.sender == sender, PatternSequence.recipient == recipient)
        .order_by(PatternSequence.test_timestamp.desc())
        .limit(limit)
    )
    return list(rows.scalars())


# ---------------------------------------------------------------- watchlist


async def get_watchlist(
    session: AsyncSession, sender: str, recipient: str, *, for_update: bool = False
) -> WatchlistEntry | None:
    stmt = select(WatchlistEntry).where(WatchlistEntry.sender == sender, WatchlistEntry.recipient == recipient)
    if for_update and session.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def all_watchlist(session: AsyncSession) -> list[WatchlistEntry]:
    return list((await session.execute(select(WatchlistEntry))).scalars())


async def list_watchlist(
    session: AsyncSession, statuses: Sequence[str] | None, offset: int, limit: int
) -> tuple[list[WatchlistEntry], int]:
    cond = WatchlistEntry.status.in_(list(statuses)) if statuses else True
    total = (await session.execute(select(func.count()).select_from(WatchlistEntry).where(cond))).scalar_one()
    rows = await session.execute(
        select(WatchlistEntry)
        .where(cond)
        .order_by(WatchlistEntry.status, WatchlistEntry.confidence_score.desc(), WatchlistEntry.id)
        .offset(offset)
        .limit(limit)
    )
    return list(rows.scalars()), int(total)


async def watchlist_counts(session: AsyncSession) -> dict[str, int]:
    rows = await session.execute(select(WatchlistEntry.status, func.count()).group_by(WatchlistEntry.status))
    return {r[0]: int(r[1]) for r in rows}


# ---------------------------------------------------------------- test / follow-up events


async def insert_test_event(session: AsyncSession, **values) -> int | None:
    stmt = (
        _insert(session, TestEvent)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["transaction_hash", "event_index"])
        .returning(TestEvent.id)
    )
    row = (await session.execute(stmt)).first()
    return row.id if row else None


async def get_test_event_for_tx(session: AsyncSession, tx_hash: str, event_index: int) -> TestEvent | None:
    return (
        await session.execute(
            select(TestEvent).where(TestEvent.transaction_hash == tx_hash, TestEvent.event_index == event_index)
        )
    ).scalar_one_or_none()


async def pending_test_events(
    session: AsyncSession, sender: str, recipient: str, at: datetime, exclude_tx_id: int
) -> list[TestEvent]:
    rows = await session.execute(
        select(TestEvent)
        .where(
            TestEvent.sender == sender,
            TestEvent.recipient == recipient,
            TestEvent.status == TestEventStatus.PENDING.value,
            TestEvent.tx_timestamp <= at,
            TestEvent.expires_at >= at,
            or_(TestEvent.transaction_id.is_(None), TestEvent.transaction_id != exclude_tx_id),
        )
        .order_by(TestEvent.tx_timestamp.desc(), TestEvent.id.desc())
    )
    return list(rows.scalars())


async def count_test_events_since(session: AsyncSession, sender: str, recipient: str, since: datetime) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(TestEvent)
                .where(TestEvent.sender == sender, TestEvent.recipient == recipient, TestEvent.tx_timestamp >= since)
            )
        ).scalar_one()
    )


async def set_test_event_status(session: AsyncSession, ids: Sequence[int], status: str) -> None:
    if ids:
        await session.execute(update(TestEvent).where(TestEvent.id.in_(list(ids))).values(status=status))


async def expire_test_events(session: AsyncSession, now: datetime) -> list[tuple[str, str]]:
    res = await session.execute(
        update(TestEvent)
        .where(TestEvent.status == TestEventStatus.PENDING.value, TestEvent.expires_at < now)
        .values(status=TestEventStatus.EXPIRED.value)
        .returning(TestEvent.sender, TestEvent.recipient)
    )
    return list({(r.sender, r.recipient) for r in res})


async def cancel_test_events_for_txs(session: AsyncSession, tx_ids: Sequence[int]) -> None:
    if tx_ids:
        await session.execute(
            update(TestEvent)
            .where(TestEvent.transaction_id.in_(list(tx_ids)), TestEvent.status == TestEventStatus.PENDING.value)
            .values(status=TestEventStatus.CANCELLED.value)
        )


async def insert_followup(session: AsyncSession, **values) -> int | None:
    stmt = _insert(session, FollowupEvent).values(**values).on_conflict_do_nothing().returning(FollowupEvent.id)
    row = (await session.execute(stmt)).first()
    return row.id if row else None


# ---------------------------------------------------------------- alerts


async def insert_alert(session: AsyncSession, **values) -> int | None:
    values.setdefault("status", AlertStatus.PENDING.value)
    values.setdefault("attempts", 0)
    stmt = (
        _insert(session, Alert)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["dedup_key"])
        .returning(Alert.id)
    )
    row = (await session.execute(stmt)).first()
    return row.id if row else None


async def next_due_alert(
    session: AsyncSession, now: datetime, *, ignore_schedule: bool = False, skip: set[int] | None = None
) -> Alert | None:
    cond = [Alert.status == AlertStatus.PENDING.value]
    if skip:
        cond.append(Alert.id.not_in(list(skip)))
    if not ignore_schedule:
        cond.append(or_(Alert.next_attempt_at.is_(None), Alert.next_attempt_at <= now))
    return (
        await session.execute(select(Alert).where(and_(*cond)).order_by(Alert.priority, Alert.id).limit(1))
    ).scalar_one_or_none()


async def reset_stuck_alerts(session: AsyncSession) -> int:
    res = await session.execute(
        update(Alert).where(Alert.status == AlertStatus.SENDING.value).values(status=AlertStatus.PENDING.value)
    )
    return res.rowcount or 0


async def alerts_by_type(session: AsyncSession) -> dict[str, dict[str, int]]:
    rows = await session.execute(select(Alert.alert_type, Alert.status, func.count()).group_by(Alert.alert_type, Alert.status))
    out: dict[str, dict[str, int]] = {}
    for t, s, c in rows:
        out.setdefault(t, {})[s] = int(c)
    return out


async def recent_latencies(session: AsyncSession, alert_type: str, limit: int = 200) -> list[int]:
    rows = await session.execute(
        select(Alert.total_detection_latency_ms)
        .where(Alert.alert_type == alert_type, Alert.total_detection_latency_ms.is_not(None))
        .order_by(Alert.id.desc())
        .limit(limit)
    )
    return [int(r[0]) for r in rows]


# ---------------------------------------------------------------- collector state


async def get_state(session: AsyncSession, key: str) -> str | None:
    row = await session.get(CollectorState, key)
    return row.value if row else None


async def set_state(session: AsyncSession, key: str, value: str, now: datetime) -> None:
    stmt = _insert(session, CollectorState).values(key=key, value=value, updated_at=now)
    stmt = stmt.on_conflict_do_update(index_elements=["key"], set_={"value": value, "updated_at": now})
    await session.execute(stmt)


async def table_counts(session: AsyncSession) -> dict[str, int]:
    """Row counts.  On PostgreSQL the two big tables use the planner estimate
    (instant) instead of COUNT(*) over tens of millions of rows."""
    out = {}
    estimated = {"transactions", "wallet_pairs"} if session.bind.dialect.name == "postgresql" else set()
    for name, model in (
        ("transactions", Transaction),
        ("wallet_pairs", WalletPair),
        ("pattern_sequences", PatternSequence),
        ("test_events", TestEvent),
        ("followup_events", FollowupEvent),
        ("alerts", Alert),
    ):
        if name in estimated:
            est = (await session.execute(text("SELECT reltuples::bigint FROM pg_class WHERE relname = :t"), {"t": name})).scalar()
            if est is not None and est > 100_000:
                out[name] = int(est)
                continue
        out[name] = int((await session.execute(select(func.count()).select_from(model))).scalar_one())
    return out
