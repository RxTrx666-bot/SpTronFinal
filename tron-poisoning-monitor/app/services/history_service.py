"""Initial historical scan of a monitored wallet.

When a wallet is added, its complete token transfer history (as far back as the
API allows, or ``HISTORY_DAYS``) is fetched page by page and stored.  Every
payment the wallet made updates the ``historical_recipients`` aggregate
exactly once (idempotent on ``transfer_key``).

The scan only covers transfers up to the moment the wallet was added; newer
transfers belong to the live monitor, so a poisoning payment made while the
scan is running is still analysed (and alerted) in real time.

Progress is persisted after every page (``history_cursor_ms``), so a restart
resumes the scan instead of starting over.  When the scan completes, a
chronological retrospective analysis finds past poisoning events and a
summary is sent to Telegram.
"""

from __future__ import annotations

from app import repository as repo
from app.database import with_db_retry
from app.domain import AnalysisStatus, HistoryStatus, WalletStatus
from app.models import PoisoningEvent
from app.services.notifier import Notifier
from app.services.poisoning_detector import LockManager, PoisoningDetector, WalletRegistry
from app.utils.address import short
from app.utils.amounts import format_amount
from app.utils.clock import Clock, as_utc, from_ms, to_ms
from app.utils.logging import get_logger
from app.utils.ratelimit import PRIORITY_HISTORY

log = get_logger(__name__)


class HistoryService:
    def __init__(
        self, settings, session_factory, source, clock: Clock, registry: WalletRegistry, detector: PoisoningDetector, notifier: Notifier, locks: LockManager
    ) -> None:
        self.s = settings
        self.sf = session_factory
        self.source = source
        self.clock = clock
        self.registry = registry
        self.detector = detector
        self.notifier = notifier
        self.locks = locks

    async def _ingest_page(self, wallet: str, transfers) -> int:
        """Store one history page; apply aggregates for new payments by monitored wallets."""
        now = self.clock.now()
        holders = {wallet} | {t.from_address for t in transfers if self.registry.is_monitored(t.from_address)}
        async with self.locks.hold(holders):
            async with self.sf() as s, s.begin():
                new = await repo.insert_transfers(s, transfers, source="HISTORY", detected_at=now, now=now, analysis_status=AnalysisStatus.DONE.value)
                for _id, t in new:
                    if (
                        t.amount > 0
                        and t.from_address != t.to_address
                        and self.registry.is_monitored(t.from_address)
                        and t.token_contract in self.s.tokens_by_contract
                    ):
                        await repo.apply_payment(
                            s, t.from_address, t.to_address, t.token_contract, t.amount, from_ms(t.block_timestamp_ms), now, self.s.candidate_key_length
                        )
        return len(new)

    async def scan(self, address: str) -> dict:
        token = self.s.primary_token
        async with self.sf() as s, s.begin():
            w = await repo.get_wallet(s, address)
            if w is None or w.status == WalletStatus.REMOVED.value:
                return {"skipped": True}
            if w.history_status == HistoryStatus.COMPLETE.value:
                return {"already_complete": True}
            w.history_status = HistoryStatus.RUNNING.value
            w.history_started_at = w.history_started_at or self.clock.now()
            w.history_error = None
            max_ts = to_ms(as_utc(w.added_at))
            cursor = w.history_cursor_ms
            scanned = w.history_transfers_scanned
        if cursor is None and self.s.history_days > 0:
            cursor = max_ts - self.s.history_days * 86_400_000
        log.info("HISTORY_SCAN_STARTED", wallet=address, resume_from=cursor, scanned=scanned)

        fp = None
        truncated = False
        new_total = 0
        while True:
            page, fp = await self.source.get_trc20_transfers(
                address,
                token.contract,
                min_timestamp_ms=cursor,
                max_timestamp_ms=max_ts,
                only_confirmed=True,
                order="asc",
                fingerprint=fp,
                limit=self.s.tron_page_limit,
                priority=PRIORITY_HISTORY,
            )
            if page:
                new_total += await with_db_retry(lambda p=page: self._ingest_page(address, p), what="history_page")
                scanned += len(page)
                last_ts = max(t.block_timestamp_ms for t in page)

                async def save(last=last_ts, n=scanned, f=fp):
                    async with self.sf() as s, s.begin():
                        w = await repo.get_wallet(s, address)
                        w.history_cursor_ms = last
                        w.history_fingerprint = f
                        w.history_transfers_scanned = n
                        w.updated_at = self.clock.now()

                await with_db_retry(save, what="history_cursor")
            if not fp or not page:
                break
            if scanned >= self.s.history_max_transfers:
                truncated = True
                log.warning("HISTORY_SCAN_TRUNCATED", wallet=address, scanned=scanned, limit=self.s.history_max_transfers)
                break

        async with self.sf() as s, s.begin():
            w = await repo.get_wallet(s, address)
            w.history_status = HistoryStatus.COMPLETE.value
            w.history_completed_at = self.clock.now()
            w.history_truncated = truncated
            w.updated_at = self.clock.now()
        log.info("HISTORY_SCAN_COMPLETE", wallet=address, scanned=scanned, new=new_total, truncated=truncated)

        summary = {"scanned": scanned, "new": new_total, "truncated": truncated}
        if self.s.retrospective_analysis:
            summary["retrospective"] = await self.detector.retrospective(address)
        await self._send_summary(address, summary)
        return summary

    async def _send_summary(self, address: str, summary: dict) -> None:
        token = self.s.primary_token
        retro = summary.get("retrospective") or {}
        async with self.sf() as s, s.begin():
            w = await repo.get_wallet(s, address)
            top = await repo.top_recipients(s, address, token.contract, limit=5)
            n_rec = await repo.count_recipients(s, address)
            lines = [
                "<b>📚 HISTORY SCAN COMPLETE</b>",
                f"<code>{address}</code>" + (f" ({w.label})" if w and w.label else ""),
                "",
                f"Transfers scanned: {summary['scanned']}" + (" (limit reached - history truncated)" if summary.get("truncated") else ""),
                f"Distinct {token.symbol} recipients: {n_rec}",
            ]
            if top:
                lines.append("")
                lines.append("<b>Top recipients:</b>")
                for r in top:
                    lines.append(
                        f"• <code>{short(r.recipient_wallet, 8, 6)}</code> — {r.transaction_count}× / {format_amount(r.total_amount, token.decimals)} {token.symbol}"
                    )
            succ, cand, att = retro.get("successful", []), retro.get("candidates", []), retro.get("attempts", [])
            if retro:
                lines.append("")
                lines.append(
                    f"Historical analysis: {len(succ)} possible successful poisoning event(s), {len(cand)} candidate(s), {len(att)} poisoning attempt(s)."
                )
                if succ or cand:
                    for eid in (succ + cand)[:10]:
                        ev = await s.get(PoisoningEvent, eid)
                        lines.append(
                            f"• <code>{ev.case_id}</code> {format_amount(ev.amount, ev.token_decimals)} {ev.token_symbol} → <code>{short(ev.suspicious_recipient, 8, 6)}</code> ({ev.confidence}/100)"
                        )
                    lines.append("Use /case &lt;CASE_ID&gt; for details.")
            lines.append("")
            lines.append("✅ Live monitoring is active for this wallet.")
            if self.s.notify_historical_events or succ or cand:
                await self.notifier.text_alert(s, f"history:{address}:{int(self.clock.now().timestamp())}", "\n".join(lines), "HISTORY_SUMMARY")
        self.notifier.wake()
