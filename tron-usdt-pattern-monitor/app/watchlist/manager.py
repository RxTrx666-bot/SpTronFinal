"""Automatic watchlist management.

Turns a freshly learned ``PatternModel`` into a watchlist entry and drives the
state machine::

    (no entry) --2 consistent sequences--> CANDIDATE --enough evidence--> ACTIVE
    ACTIVE --evidence degrades--> WEAKENED --evidence recovers--> ACTIVE
    any    --no sequence for PATTERN_EXPIRY_DAYS--> EXPIRED
    ACTIVE --test flood / operator--> PAUSED --pause ends--> re-evaluated

No manual wallet entry exists anywhere: entries are created only from
behaviour observed on-chain.  Historical data is never deleted on a state change.
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings
from app.database import repository as repo
from app.database.models import WatchlistEntry
from app.detector.pattern_engine import PatternModel, _ratio_to_decimal
from app.domain import ALERT_PRIORITY, AlertType, WatchlistStatus
from app.logging_setup import get_logger
from app.telegram.messages import MessageFormatter
from app.watchlist.model import WatchlistSnapshot

log = get_logger(__name__)


def decide_status(
    entry: WatchlistEntry | None, model: PatternModel | None, now: datetime, settings: Settings
) -> WatchlistStatus | None:
    paused = (
        entry is not None
        and entry.status == WatchlistStatus.PAUSED.value
        and (entry.manual_pause or (entry.pause_until is not None and entry.pause_until > now))
    )
    if model is None or not model.inliers:
        if entry is None:
            return None
        if paused:
            return WatchlistStatus.PAUSED
        last = entry.last_sequence_at
        if last is None or (now - last).days > settings.pattern_expiry_days:
            return WatchlistStatus.EXPIRED
        return WatchlistStatus.WEAKENED
    if paused:
        return WatchlistStatus.PAUSED
    if model.expired:
        return WatchlistStatus.EXPIRED if entry is not None else None
    if model.qualifies_active:
        return WatchlistStatus.ACTIVE
    if entry is not None and entry.activated_at is not None:
        return WatchlistStatus.WEAKENED
    if model.qualifies_candidate:
        return WatchlistStatus.CANDIDATE
    if entry is not None:
        return WatchlistStatus.WEAKENED
    return None


class WatchlistManager:
    def __init__(self, settings: Settings, formatter: MessageFormatter) -> None:
        self.settings = settings
        self.formatter = formatter

    async def apply_model(
        self,
        session: AsyncSession,
        sender: str,
        recipient: str,
        model: PatternModel | None,
        now: datetime,
        *,
        silent: bool = False,
    ) -> WatchlistSnapshot | None:
        entry = await repo.get_watchlist(session, sender, recipient, for_update=True)
        new_status = decide_status(entry, model, now, self.settings)
        if new_status is None:
            return None

        created = entry is None
        old_status = entry.status if entry else None
        if entry is None:
            entry = WatchlistEntry(sender=sender, recipient=recipient, created_at=now, activation_count=0)
            session.add(entry)

        if model is not None and model.inliers:
            self._copy_model(entry, model)
            entry.status_reason = "; ".join(model.reasons) or None
        else:
            entry.successful_sequences = 0
            entry.status_reason = "no TEST → LARGE sequences left in the analysis window"
            entry.confidence = "LOW"
            entry.confidence_score = 0.0

        entry.status = new_status.value
        # PAUSED -> ACTIVE is a resume, not a new activation (no repeat announcement).
        resumed = old_status == WatchlistStatus.PAUSED.value
        if new_status == WatchlistStatus.ACTIVE and old_status != WatchlistStatus.ACTIVE.value and not resumed:
            entry.activated_at = now
            entry.activation_count = (entry.activation_count or 0) + 1
        if new_status != WatchlistStatus.PAUSED and old_status == WatchlistStatus.PAUSED.value:
            entry.pause_until = None
        entry.updated_at = now
        await session.flush()

        snap = WatchlistSnapshot.from_entry(entry)
        if created:
            log.info(
                "Automatic watchlist entry created",
                sender=sender,
                recipient=recipient,
                status=new_status.value,
                sequences=entry.successful_sequences,
                confidence=entry.confidence,
            )
        elif old_status != new_status.value:
            log.info(
                "Watchlist status changed",
                sender=sender,
                recipient=recipient,
                old=old_status,
                new=new_status.value,
                reason=entry.status_reason,
            )

        if not silent and not resumed:
            await self._notify(session, snap, created=created, old_status=old_status, now=now)
        return snap

    def _copy_model(self, e: WatchlistEntry, m: PatternModel) -> None:
        e.confidence = m.confidence.value
        e.confidence_score = m.score
        e.pattern_strength = m.pattern_strength
        e.typical_test_amount_raw = m.test_mean_raw
        e.median_test_amount_raw = m.test_median_raw
        e.test_amount_min_raw = m.test_min_raw
        e.test_amount_max_raw = m.test_max_raw
        e.match_low_raw = m.match_low_raw
        e.match_high_raw = m.match_high_raw
        e.typical_large_amount_raw = m.large_mean_raw
        e.median_large_amount_raw = m.large_median_raw
        e.large_amount_min_raw = m.large_min_raw
        e.large_amount_max_raw = m.large_max_raw
        e.typical_test_to_large_ratio = _ratio_to_decimal(m.ratio_median)
        e.typical_followup_seconds = m.followup_median_seconds
        e.followup_window_seconds = m.followup_window_seconds
        e.successful_sequences = m.successful_sequences
        e.total_sequences = len(m.sequences)
        e.success_rate = m.success_rate
        e.last_test_transfer = max(filter(None, [e.last_test_transfer, m.last_test_at]))
        e.last_large_transfer = max(filter(None, [e.last_large_transfer, m.last_large_at]))
        e.last_sequence_at = m.last_sequence_at
        e.model_json = json.dumps(
            {
                "components": m.components.as_dict(),
                "reasons": m.reasons,
                "recent_large_amounts": m.recent_large_amounts(),
                "recent_test_amounts": m.recent_test_amounts(),
                "followup_p80_seconds": m.followup_p80_seconds,
                "followup_max_seconds": m.followup_max_seconds,
                "effective_sequences": m.effective_sequences,
                "inlier_fraction": m.inlier_fraction,
                "relationship_consistency": m.relationship_consistency,
                "judged_tests": m.judged_tests,
                "successful_tests": m.successful_tests,
            }
        )

    async def _notify(
        self, session: AsyncSession, snap: WatchlistSnapshot, *, created: bool, old_status: str | None, now: datetime
    ) -> None:
        if snap.status == WatchlistStatus.ACTIVE and old_status != WatchlistStatus.ACTIVE.value:
            e_count = await self._activation_count(session, snap)
            await repo.insert_alert(
                session,
                dedup_key=f"ACTIVATED:{snap.sender}:{snap.recipient}:{e_count}",
                alert_type=AlertType.WATCHLIST_ACTIVATED.value,
                priority=ALERT_PRIORITY[AlertType.WATCHLIST_ACTIVATED],
                sender=snap.sender,
                recipient=snap.recipient,
                amount_raw=snap.typical_test_raw,
                message_text=self.formatter.activated_alert(snap),
                processing_end_time=now,
                created_at=now,
            )
            log.info("Watchlist activated", sender=snap.sender, recipient=snap.recipient, confidence=snap.confidence)
        elif created and snap.status == WatchlistStatus.CANDIDATE and self.settings.notify_new_patterns:
            await repo.insert_alert(
                session,
                dedup_key=f"NEW_PATTERN:{snap.sender}:{snap.recipient}",
                alert_type=AlertType.NEW_PATTERN.value,
                priority=ALERT_PRIORITY[AlertType.NEW_PATTERN],
                sender=snap.sender,
                recipient=snap.recipient,
                amount_raw=snap.typical_test_raw,
                message_text=self.formatter.new_pattern_alert(snap),
                processing_end_time=now,
                created_at=now,
            )

    async def _activation_count(self, session: AsyncSession, snap: WatchlistSnapshot) -> int:
        entry = await repo.get_watchlist(session, snap.sender, snap.recipient)
        return entry.activation_count if entry else 1

    async def pause(
        self, session: AsyncSession, sender: str, recipient: str, *, until: datetime | None, reason: str, now: datetime
    ) -> WatchlistSnapshot | None:
        entry = await repo.get_watchlist(session, sender, recipient, for_update=True)
        if entry is None:
            return None
        entry.status = WatchlistStatus.PAUSED.value
        entry.pause_until = until
        entry.manual_pause = until is None
        entry.status_reason = reason
        entry.updated_at = now
        await session.flush()
        log.warning("Watchlist entry paused", sender=sender, recipient=recipient, until=until, reason=reason)
        return WatchlistSnapshot.from_entry(entry)

    async def resume(self, session: AsyncSession, sender: str, recipient: str, now: datetime) -> bool:
        entry = await repo.get_watchlist(session, sender, recipient, for_update=True)
        if entry is None:
            return False
        entry.manual_pause = False
        entry.pause_until = now
        entry.updated_at = now
        await session.flush()
        return True
