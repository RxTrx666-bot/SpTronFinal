"""Turns incidents into durable Telegram outbox rows (table ``alerts``).

Alerts are written in the SAME database transaction that creates the
incident, keyed by a deterministic ``dedup_key``.  The alert worker delivers
them (with retries) and the unique key guarantees an incident is never
announced twice - across restarts, replays and duplicate API data.
"""

from __future__ import annotations

import asyncio
import time

from sqlalchemy.ext.asyncio import AsyncSession

from app import repository as repo
from app.domain import EventType
from app.services import report_service as rs
from app.utils.clock import Clock

CONSOLE_CHAT = 0  # used when no Telegram chat is configured (logged / printed only)


class Notifier:
    def __init__(self, settings, clock: Clock) -> None:
        self.s = settings
        self.clock = clock
        self.wakeup = asyncio.Event()

    @property
    def chats(self) -> list[int]:
        return self.s.alert_chat_ids or [CONSOLE_CHAT]

    def wake(self) -> None:
        self.wakeup.set()

    async def event_alert(self, session: AsyncSession, event_id: int, kind: str, suffix: str | None = None) -> int:
        """kind: SUCCESS / CANDIDATE / ATTEMPT / UPGRADE / UPDATE / TRACE / DROPPED / CONFIRMED."""
        b = await rs.load_bundle(session, event_id)
        markup = None
        if kind in ("SUCCESS", "UPGRADE", "CANDIDATE", "ATTEMPT"):
            text = rs.telegram_alert(b)
            if kind in ("SUCCESS", "UPGRADE", "CANDIDATE"):
                markup = rs.alert_keyboard(event_id, x_enabled=self.s.x_enabled)
        elif kind == "TRACE":
            text = rs.trace_message(b)
        else:
            text = self._update_text(b, kind)
        count = 0
        for chat in self.chats:
            dedup = f"event:{event_id}:{kind}:{chat}"
            if suffix:
                dedup += f":{suffix}"
            elif kind in ("TRACE", "UPDATE"):
                dedup += f":{time.time_ns()}"
            payload = {"text": text, "parse_mode": "HTML"}
            if markup:
                payload["reply_markup"] = markup
            if await repo.enqueue_alert(session, dedup_key=dedup, chat_id=chat, alert_type=kind, payload=payload, now=self.clock.now(), event_id=event_id):
                count += 1
        return count

    def _update_text(self, b: rs.CaseBundle, kind: str) -> str:
        ev = b.event
        head = {
            "UPDATE": "🧾 EVIDENCE UPDATE",
            "DROPPED": "↩️ TRANSACTION NOT CONFIRMED",
            "CONFIRMED": "✅ TRANSACTION CONFIRMED",
        }.get(kind, kind)
        lines = [f"<b>{head}</b> — <code>{ev.case_id}</code>", ""]
        if kind == "DROPPED":
            lines.append("The transaction was not found in a solidified block and was marked DROPPED. Treat the earlier alert accordingly.")
        elif kind == "CONFIRMED":
            lines.append(f"Transaction is now in solidified block {ev.block_number}.")
        else:
            lines.append(f"Confidence: <b>{ev.confidence}/100</b> ({rs.STATUS_LINE[ev.event_type]})")
            lines.append(f"Poisoning transaction observed: {ev.poisoning_tx_observed}")
            if ev.forwarding_summary:
                lines.append(f"Forwarding: {rs._h(ev.forwarding_summary)}")
            extra = [
                e
                for e in b.evidence
                if e.kind == "FACT" and e.evidence_type in ("PRIOR_DUST", "MULTI_VICTIM_DUST", "OTHER_VICTIMS", "FORWARDING", "MANY_SENDERS")
            ]
            for e in extra[:8]:
                lines.append(f"• {rs._h(e.description)}")
        return "\n".join(lines)

    async def text_alert(self, session: AsyncSession, dedup_key: str, text: str, alert_type: str = "SYSTEM", markup: dict | None = None) -> None:
        for chat in self.chats:
            payload = {"text": text, "parse_mode": "HTML"}
            if markup:
                payload["reply_markup"] = markup
            await repo.enqueue_alert(session, dedup_key=f"{dedup_key}:{chat}", chat_id=chat, alert_type=alert_type, payload=payload, now=self.clock.now())

    @staticmethod
    def kind_for(event_type: str) -> str:
        return {
            EventType.SUCCESSFUL_POISONING_EVENT.value: "SUCCESS",
            EventType.POISONING_CANDIDATE.value: "CANDIDATE",
            EventType.POISONING_ATTEMPT.value: "ATTEMPT",
        }[event_type]
