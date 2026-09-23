"""Transaction processing pipeline.

    collector -> dedup -> store (idempotent) -> pair index -> watchlist match -> alert outbox
                                                    \\-> pattern analysis queue (learning)

Critical path (latency-sensitive) for a transfer of a WATCHLIST pair:

1. one DB transaction stores the transfer (``processed = false``) and updates
   the Sender -> Recipient statistics;
2. ``TransactionMatcher`` immediately checks it against the relationship's
   learned test band / pending follow-ups and writes the test event + alert
   row in a second short transaction, then wakes the Telegram dispatcher.

Telegram I/O never happens inside this path (outbox pattern), so a slow or
failing Telegram API cannot block blockchain processing.  If the process dies
between steps 1 and 2 the transfer still has ``processed = false`` and the
maintenance loop re-runs matching on restart - unique keys make that safe.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.amounts import fmt_usdt
from app.clock import Clock
from app.config.settings import Settings
from app.database import repository as repo
from app.database.models import WatchlistEntry
from app.database.session import is_transient_db_error
from app.detector.followup_detector import select_followup
from app.detector.test_detector import match_test_transfer
from app.domain import ALERT_PRIORITY, AlertType, TestEventStatus, TransferEvent, TxStatus
from app.logging_setup import get_logger
from app.processing.deduplication import SeenCache, alert_dedup_key
from app.telegram.messages import MessageFormatter
from app.watchlist.manager import WatchlistManager
from app.watchlist.model import WatchlistCache, WatchlistSnapshot

log = get_logger(__name__)


@dataclass
class PipelineStats:
    events_seen: int = 0
    events_stored: int = 0
    confirmations: int = 0
    test_alerts: int = 0
    followup_alerts: int = 0
    last_event_time: datetime | None = None
    last_batch_at: datetime | None = None
    rejected: dict[str, int] = field(default_factory=dict)


class TransactionMatcher:
    """Matches stored transfers against the automatic watchlist."""

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker,
        clock: Clock,
        cache: WatchlistCache,
        manager: WatchlistManager,
        formatter: MessageFormatter,
        notify: Callable[[], None],
        on_followup: Callable[[tuple[str, str]], None] | None = None,
        stats: PipelineStats | None = None,
    ) -> None:
        self.s = settings
        self.sf = session_factory
        self.clock = clock
        self.cache = cache
        self.manager = manager
        self.fmt = formatter
        self.notify = notify
        self.on_followup = on_followup
        self.stats = stats or PipelineStats()

    async def match(self, tx: repo.StoredTx, processing_start: datetime) -> None:
        try:
            await self._match(tx, processing_start)
        except Exception as exc:
            if is_transient_db_error(exc):
                raise  # stays processed=false -> retried by maintenance
            log.exception("Watchlist matching failed; transfer skipped", tx=tx.transaction_hash)
            async with self.sf() as s, s.begin():
                await repo.mark_processed(s, tx.id)

    async def _match(self, tx: repo.StoredTx, processing_start: datetime) -> None:
        snap = self.cache.get(tx.sender, tx.recipient)
        now = self.clock.now()
        confirmed = tx.status == TxStatus.CONFIRMED.value
        too_old = (now - tx.timestamp) > timedelta(minutes=self.s.alert_max_tx_age_minutes)
        paused_snap: WatchlistSnapshot | None = None
        created = False
        followed_pair = None

        async with self.sf() as s, s.begin():
            if snap is None:
                await repo.mark_processed(s, tx.id)
                return
            m = match_test_transfer(snap, tx.amount_raw, now, alert_on_weakened=self.s.alert_on_weakened)
            if m.matched and not tx.upgraded:
                if too_old:
                    log.info("Known test pattern matched but transfer too old to alert", tx=tx.transaction_hash)
                else:
                    since = tx.timestamp - timedelta(hours=1)
                    recent = await repo.count_test_events_since(s, tx.sender, tx.recipient, since)
                    if recent >= self.s.flood_max_tests_per_hour:
                        paused_snap = await self.manager.pause(
                            s,
                            tx.sender,
                            tx.recipient,
                            until=now + timedelta(minutes=self.s.flood_pause_minutes),
                            reason=f"test-transfer flood: {recent} matching tests within 1 hour",
                            now=now,
                        )
                    else:
                        created = await self._create_test_alert(s, snap, tx, processing_start, confirmed)
            elif not m.matched:
                pending = await repo.pending_test_events(s, tx.sender, tx.recipient, tx.timestamp, tx.id)
                te = select_followup(pending, tx.amount_raw, tx.timestamp, self.s.ratio_fraction)
                if te is not None and not too_old:
                    followed_pair = await self._create_followup(s, te, pending, tx, processing_start, confirmed)
            if tx.upgraded and self.s.send_confirmation_alerts:
                te = await repo.get_test_event_for_tx(s, tx.transaction_hash, tx.event_index)
                if te is not None:
                    await repo.insert_alert(
                        s,
                        dedup_key=alert_dedup_key(AlertType.TEST_CONFIRMED, tx.transaction_hash, tx.event_index),
                        transaction_hash=tx.transaction_hash,
                        alert_type=AlertType.TEST_CONFIRMED.value,
                        priority=ALERT_PRIORITY[AlertType.TEST_CONFIRMED],
                        sender=tx.sender,
                        recipient=tx.recipient,
                        amount_raw=tx.amount_raw,
                        message_text=self.fmt.test_confirmed_alert(
                            sender=tx.sender, recipient=tx.recipient, amount_raw=tx.amount_raw, tx_hash=tx.transaction_hash
                        ),
                        created_at=now,
                    )
                    created = True
            await repo.mark_processed(s, tx.id)

        if paused_snap is not None:
            self.cache.put(paused_snap)
        if created or followed_pair:
            self.notify()
        if followed_pair and self.on_followup:
            self.on_followup(followed_pair)

    async def _create_test_alert(self, s, snap: WatchlistSnapshot, tx: repo.StoredTx, start: datetime, confirmed: bool) -> bool:
        te_id = await repo.insert_test_event(
            s,
            watchlist_id=snap.id,
            sender=tx.sender,
            recipient=tx.recipient,
            transaction_id=tx.id,
            transaction_hash=tx.transaction_hash,
            event_index=tx.event_index,
            amount_raw=tx.amount_raw,
            tx_timestamp=tx.timestamp,
            detected_at=tx.detected_at,
            expires_at=tx.timestamp + timedelta(seconds=snap.followup_window_seconds),
            status=TestEventStatus.PENDING.value,
            created_at=self.clock.now(),
        )
        if te_id is None:
            return False  # already alerted for this transfer
        text = self.fmt.test_alert(
            snap,
            tx_hash=tx.transaction_hash,
            amount_raw=tx.amount_raw,
            tx_time=tx.timestamp,
            detected_at=tx.detected_at,
            confirmed=confirmed,
        )
        await repo.insert_alert(
            s,
            dedup_key=alert_dedup_key(AlertType.TEST_DETECTED, tx.transaction_hash, tx.event_index),
            transaction_hash=tx.transaction_hash,
            alert_type=AlertType.TEST_DETECTED.value,
            priority=ALERT_PRIORITY[AlertType.TEST_DETECTED],
            sender=tx.sender,
            recipient=tx.recipient,
            amount_raw=tx.amount_raw,
            message_text=text,
            blockchain_event_time=tx.timestamp,
            blockchain_detection_time=tx.detected_at,
            processing_start_time=start,
            processing_end_time=self.clock.now(),
            created_at=self.clock.now(),
        )
        await s.execute(
            update(WatchlistEntry).where(WatchlistEntry.id == snap.id).values(last_test_transfer=tx.timestamp)
        )
        self.stats.test_alerts += 1
        log.info(
            "Known watchlist test detected",
            sender=tx.sender,
            recipient=tx.recipient,
            amount=fmt_usdt(tx.amount_raw),
            learned_range=f"{fmt_usdt(snap.test_min_raw)}-{fmt_usdt(snap.test_max_raw)}",
            status="CONFIRMED" if confirmed else "UNCONFIRMED",
            tx=tx.transaction_hash,
        )
        return True

    async def _create_followup(self, s, te, pending, tx: repo.StoredTx, start: datetime, confirmed: bool):
        dt = max(0, int((tx.timestamp - te.tx_timestamp).total_seconds()))
        fid = await repo.insert_followup(
            s,
            test_event_id=te.id,
            sender=tx.sender,
            recipient=tx.recipient,
            large_transaction_id=tx.id,
            large_transaction_hash=tx.transaction_hash,
            large_event_index=tx.event_index,
            test_amount_raw=te.amount_raw,
            large_amount_raw=tx.amount_raw,
            amount_ratio=_ratio(tx.amount_raw, te.amount_raw),
            time_difference_seconds=dt,
            created_at=self.clock.now(),
        )
        if fid is None:
            return None
        await repo.set_test_event_status(s, [te.id], TestEventStatus.FOLLOWED_UP.value)
        older = [p.id for p in pending if p.id != te.id and p.tx_timestamp <= te.tx_timestamp]
        await repo.set_test_event_status(s, older, TestEventStatus.SUPERSEDED.value)
        await repo.insert_alert(
            s,
            dedup_key=alert_dedup_key(AlertType.LARGE_FOLLOWUP, tx.transaction_hash, tx.event_index),
            transaction_hash=tx.transaction_hash,
            alert_type=AlertType.LARGE_FOLLOWUP.value,
            priority=ALERT_PRIORITY[AlertType.LARGE_FOLLOWUP],
            sender=tx.sender,
            recipient=tx.recipient,
            amount_raw=tx.amount_raw,
            message_text=self.fmt.followup_alert(
                sender=tx.sender,
                recipient=tx.recipient,
                test_amount_raw=te.amount_raw,
                test_tx_hash=te.transaction_hash,
                amount_raw=tx.amount_raw,
                tx_hash=tx.transaction_hash,
                dt_seconds=dt,
                tx_time=tx.timestamp,
                detected_at=tx.detected_at,
                confirmed=confirmed,
            ),
            blockchain_event_time=tx.timestamp,
            blockchain_detection_time=tx.detected_at,
            processing_start_time=start,
            processing_end_time=self.clock.now(),
            created_at=self.clock.now(),
        )
        await s.execute(
            update(WatchlistEntry)
            .where(WatchlistEntry.sender == tx.sender, WatchlistEntry.recipient == tx.recipient)
            .values(last_large_transfer=tx.timestamp)
        )
        self.stats.followup_alerts += 1
        log.info(
            "Large follow-up detected",
            sender=tx.sender,
            recipient=tx.recipient,
            test_amount=fmt_usdt(te.amount_raw),
            amount=fmt_usdt(tx.amount_raw),
            seconds_after_test=dt,
            tx=tx.transaction_hash,
        )
        return tx.pair


def _ratio(a: int, b: int):
    from app.detector.pattern_engine import _ratio_to_decimal
    from fractions import Fraction

    return _ratio_to_decimal(Fraction(a, b))


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker,
        clock: Clock,
        cache: WatchlistCache,
        matcher: TransactionMatcher,
        enqueue_analysis: Callable[[tuple[str, str]], None],
        stats: PipelineStats,
    ) -> None:
        self.s = settings
        self.sf = session_factory
        self.clock = clock
        self.cache = cache
        self.matcher = matcher
        self.enqueue_analysis = enqueue_analysis
        self.stats = stats
        self.seen = SeenCache(settings.dedup_cache_size)

    async def process(self, events: list[TransferEvent], *, live: bool = True) -> int:
        """Store and analyse a batch of decoded events.  Returns the number of new
        rows or confirmations.  Raises on database failure so the caller does not
        advance its cursor (nothing is lost)."""
        self.stats.events_seen += len(events)
        events = self.seen.filter_new(events)
        if not events:
            return 0
        start = self.clock.now()
        watch = self.cache.pairs() if live else set()
        rematch = watch if self.s.send_confirmation_alerts else set()

        async with self.sf() as s, s.begin():
            inserted = await repo.insert_transactions(
                s, events, detected_at=start, needs_matching=watch, decimals=self.s.usdt_decimals
            )
            got = {(t.transaction_hash, t.event_index) for t in inserted}
            upgraded = await repo.confirm_transactions(
                s, [e for e in events if e.confirmed and e.key not in got], now=start, rematch=rematch
            )
            newly_confirmed = [t for t in inserted if t.newly_confirmed] + upgraded
            flagged = await repo.upsert_pair_stats(
                s,
                repo.aggregate_pairs(newly_confirmed),
                now=start,
                ratio=self.s.ratio_fraction,
                flag_analysis=live,
            )
        self.seen.mark(events)

        self.stats.events_stored += len(inserted)
        self.stats.confirmations += len(upgraded)
        self.stats.last_batch_at = start
        last = max(e.timestamp for e in events)
        if self.stats.last_event_time is None or last > self.stats.last_event_time:
            self.stats.last_event_time = last
        if log.is_debug():
            for t in inserted:
                log.debug(
                    "Processing USDT transfer",
                    sender=t.sender,
                    recipient=t.recipient,
                    amount=fmt_usdt(t.amount_raw),
                    status=t.status,
                )

        if live:
            to_match = [t for t in inserted if t.pair in watch]
            to_match += [t for t in upgraded if t.pair in rematch]
            for t in to_match:
                await self.matcher.match(t, start)
            for pair in flagged:
                self.enqueue_analysis(pair)
        return len(inserted) + len(upgraded)
