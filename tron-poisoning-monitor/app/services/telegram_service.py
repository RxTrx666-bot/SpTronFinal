"""Telegram integration: Bot API transport, admin commands and alert buttons.

Only the administrators in ``TELEGRAM_ADMIN_CHAT_ID`` (user ids or chat ids,
comma-separated) can use commands or buttons; everyone else is rejected and
recorded in ``telegram_users``.

Commands: /start /help /add /remove /list /pause /resume /status
          /cases /case /recipients
Buttons:  📋 COPY CASE · 🔎 TRACE FUNDS · 📄 FULL REPORT · 🐦 PREPARE X POST
          → 📤 POST TO X (only when X_ENABLED) · CANCEL
"""

from __future__ import annotations

import asyncio
import html
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

import httpx
from sqlalchemy import func, select

from app import repository as repo
from app.domain import EventType, WalletStatus
from app.models import FundTrace, HistoricalRecipient, PoisoningEvent, TelegramUser, XPostDraft
from app.services import report_service as rs
from app.services.admin_service import AdminService
from app.services.x_service import XError, XService, validate_post
from app.utils.address import InvalidAddress, normalize_address, short
from app.utils.amounts import format_amount
from app.utils.clock import Clock, iso
from app.utils.logging import get_logger

log = get_logger(__name__)

MAX_MESSAGE = 4000
_TAG = re.compile(r"<[^>]+>")


class SendError(Exception):
    def __init__(self, message: str, *, retryable: bool = True, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class TelegramTransport(Protocol):
    async def send_message(self, chat_id: int, text: str, *, parse_mode: str | None = "HTML", reply_markup: dict | None = None) -> int | None: ...
    async def send_document(self, chat_id: int, filename: str, content: bytes, caption: str | None = None) -> int | None: ...
    async def answer_callback(self, callback_id: str, text: str | None = None) -> None: ...
    async def get_updates(self, offset: int | None, timeout: int) -> list[dict]: ...
    async def set_commands(self, commands: list[tuple[str, str]]) -> None: ...
    async def close(self) -> None: ...


class BotApiTransport:
    def __init__(self, token: str, timeout: float = 15.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(base_url=f"https://api.telegram.org/bot{token}/", timeout=httpx.Timeout(timeout), transport=transport)
        self.polls = True

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, *, json: dict | None = None, data: dict | None = None, files: dict | None = None, timeout: float | None = None) -> Any:
        try:
            kw: dict[str, Any] = {}
            if timeout:
                kw["timeout"] = timeout
            resp = await self._client.post(method, json=json, data=data, files=files, **kw)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise SendError(f"network: {type(exc).__name__}") from exc
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code == 200 and body.get("ok"):
            return body.get("result")
        desc = str(body.get("description") or resp.text)[:200]
        if resp.status_code == 429:
            raise SendError(f"rate limited: {desc}", retry_after=float((body.get("parameters") or {}).get("retry_after", 5)))
        retryable = resp.status_code >= 500 or resp.status_code in (401, 403, 404)  # config issues: retry slowly
        raise SendError(f"telegram {resp.status_code}: {desc}", retryable=retryable)

    async def send_message(self, chat_id: int, text: str, *, parse_mode: str | None = "HTML", reply_markup: dict | None = None) -> int | None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text[:4096], "disable_web_page_preview": True}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            res = await self._call("sendMessage", json=payload)
        except SendError as exc:
            if "can't parse entities" not in str(exc):
                raise
            payload.pop("parse_mode", None)
            payload["text"] = html.unescape(_TAG.sub("", text))[:4096]
            res = await self._call("sendMessage", json=payload)
        return (res or {}).get("message_id")

    async def send_document(self, chat_id: int, filename: str, content: bytes, caption: str | None = None) -> int | None:
        data = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption[:1000]
        res = await self._call("sendDocument", data=data, files={"document": (filename, content)})
        return (res or {}).get("message_id")

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        try:
            await self._call("answerCallbackQuery", json={"callback_query_id": callback_id, "text": (text or "")[:190]})
        except SendError:
            pass

    async def get_updates(self, offset: int | None, timeout: int) -> list[dict]:
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        return await self._call("getUpdates", json=payload, timeout=timeout + 10) or []

    async def set_commands(self, commands: list[tuple[str, str]]) -> None:
        await self._call("setMyCommands", json={"commands": [{"command": c, "description": d} for c, d in commands]})


class ConsoleTransport:
    """Prints messages to stdout and records them (simulation / no Telegram configured / tests)."""

    def __init__(self, echo: bool = True) -> None:
        self.echo = echo
        self.messages: list[dict] = []
        self.documents: list[dict] = []
        self.callbacks: list[tuple[str, str | None]] = []
        self.fail = False  # simulate a Telegram outage
        self.fail_count = 0
        self.updates: asyncio.Queue = asyncio.Queue()
        self.polls = False
        self._next_id = 1000

    async def close(self) -> None:
        pass

    def _check(self) -> None:
        if self.fail:
            self.fail_count += 1
            raise SendError("simulated Telegram outage")

    async def send_message(self, chat_id: int, text: str, *, parse_mode: str | None = "HTML", reply_markup: dict | None = None) -> int | None:
        self._check()
        self._next_id += 1
        self.messages.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup, "message_id": self._next_id})
        if self.echo:
            plain = html.unescape(_TAG.sub("", text))
            buttons = ""
            if reply_markup:
                buttons = "\n" + "  ".join(f"[{b['text']}]" for row in reply_markup.get("inline_keyboard", []) for b in row)
            print(f"\n──────── TELEGRAM → {chat_id} ────────\n{plain}{buttons}\n", flush=True)
        return self._next_id

    async def send_document(self, chat_id: int, filename: str, content: bytes, caption: str | None = None) -> int | None:
        self._check()
        self._next_id += 1
        self.documents.append({"chat_id": chat_id, "filename": filename, "content": content, "caption": caption})
        if self.echo:
            print(f"\n──────── TELEGRAM DOCUMENT → {chat_id}: {filename} ({len(content)} bytes) ────────", flush=True)
        return self._next_id

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        self.callbacks.append((callback_id, text))

    async def get_updates(self, offset: int | None, timeout: int) -> list[dict]:
        try:
            return [await asyncio.wait_for(self.updates.get(), timeout=0.2)]
        except asyncio.TimeoutError:
            return []

    async def set_commands(self, commands) -> None:
        pass


COMMANDS = [
    ("start", "Introduction"),
    ("help", "Show commands"),
    ("add", "/add <TRON_ADDRESS> [label] — monitor a wallet"),
    ("remove", "/remove <TRON_ADDRESS> — stop monitoring"),
    ("list", "List monitored wallets"),
    ("pause", "/pause [ADDRESS] — pause alerts (all or one wallet)"),
    ("resume", "/resume [ADDRESS] — resume alerts"),
    ("status", "System status and latency"),
    ("cases", "Recent incidents"),
    ("case", "/case <CASE_ID> — incident details"),
    ("recipients", "/recipients <ADDRESS> — top historical recipients"),
]

HELP = """<b>🛡 TRON Address-Poisoning Monitor</b> (read-only)

Detects when a monitored wallet <b>sends funds to a look-alike</b> of an address it used before — the moment an address-poisoning attack succeeds — and alerts immediately.

<b>Wallets</b>
/add &lt;ADDRESS&gt; [label] — monitor a wallet (history scan starts automatically)
/remove &lt;ADDRESS&gt; — stop monitoring
/list — monitored wallets
/pause [ADDRESS] — pause alert delivery (all, or one wallet)
/resume [ADDRESS] — resume
/recipients &lt;ADDRESS&gt; — top historical recipients

<b>Incidents</b>
/cases — recent incidents
/case &lt;CASE_ID&gt; — details and buttons

/status — health, block lag, latency

This bot never asks for private keys and never sends transactions."""


class TelegramBot:
    def __init__(
        self,
        settings,
        session_factory,
        clock: Clock,
        transport,
        admin: AdminService,
        notifier,
        x_service: XService,
        status_provider: Callable[[], Awaitable[str]],
        jobs_wakeup: asyncio.Event,
    ) -> None:
        self.s = settings
        self.sf = session_factory
        self.clock = clock
        self.t = transport
        self.admin = admin
        self.notifier = notifier
        self.x = x_service
        self.status_provider = status_provider
        self.jobs_wakeup = jobs_wakeup
        self._unauth_notified: dict[int, float] = {}

    # ----------------------------------------------------------------- auth
    def is_admin(self, user_id: int | None, chat_id: int | None) -> bool:
        admins = self.s.admin_ids
        return bool(admins) and (user_id in admins or chat_id in admins)

    async def _record_user(self, user: dict, admin: bool) -> None:
        uid = user.get("id")
        if uid is None:
            return
        now = self.clock.now()
        try:
            async with self.sf() as s, s.begin():
                row = (await s.execute(select(TelegramUser).where(TelegramUser.user_id == uid))).scalar_one_or_none()
                if row is None:
                    row = TelegramUser(user_id=uid, first_seen=now, last_seen=now, command_count=0, unauthorized_attempts=0, is_admin=admin)
                    s.add(row)
                row.username = (user.get("username") or "")[:64] or None
                row.first_name = (user.get("first_name") or "")[:128] or None
                row.is_admin = admin
                row.last_seen = now
                if admin:
                    row.command_count += 1
                else:
                    row.unauthorized_attempts += 1
        except Exception as exc:  # noqa: BLE001 - bookkeeping only
            log.warning("TELEGRAM_USER_RECORD_FAILED", error=type(exc).__name__)

    # ----------------------------------------------------------------- loop
    async def run(self, stop: asyncio.Event) -> None:
        try:
            await self.t.set_commands(COMMANDS)
        except Exception:  # noqa: BLE001
            pass
        async with self.sf() as s:
            off = await repo.get_state(s, "telegram_offset")
        offset = int(off) if off else None
        delay = 1.0
        while not stop.is_set():
            try:
                updates = await self.t.get_updates(offset, self.s.telegram_poll_timeout_seconds)
                delay = 1.0
            except SendError as exc:
                log.warning("TELEGRAM_POLL_FAILED", error=str(exc)[:120])
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            for upd in updates:
                try:
                    await self.handle_update(upd)
                except Exception as exc:  # noqa: BLE001
                    log.exception("TELEGRAM_UPDATE_FAILED", error=type(exc).__name__)
                if "update_id" in upd:
                    offset = int(upd["update_id"]) + 1
                    async with self.sf() as s, s.begin():
                        await repo.set_state(s, "telegram_offset", str(offset), self.clock.now())

    async def reply(self, chat_id: int, text: str, markup: dict | None = None) -> None:
        for chunk in split_message(text):
            try:
                await self.t.send_message(chat_id, chunk, reply_markup=markup)
            except SendError as exc:
                log.warning("TELEGRAM_REPLY_FAILED", error=str(exc)[:120])
                return
            markup = None

    async def handle_update(self, upd: dict) -> None:
        if "callback_query" in upd:
            await self.handle_callback(upd["callback_query"])
            return
        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            return
        chat_id = (msg.get("chat") or {}).get("id")
        user = msg.get("from") or {}
        admin = self.is_admin(user.get("id"), chat_id)
        await self._record_user(user, admin)
        if not admin:
            log.warning("TELEGRAM_UNAUTHORIZED", user_id=user.get("id"), username=user.get("username"), command=text.split()[0][:32])
            last = self._unauth_notified.get(user.get("id") or 0, 0)
            if self.clock.now().timestamp() - last > 3600:
                self._unauth_notified[user.get("id") or 0] = self.clock.now().timestamp()
                await self.reply(chat_id, "⛔ You are not authorized to use this bot.")
            return
        await self.reply(chat_id, await self.command(text, user_id=user.get("id")))

    async def command(self, text: str, user_id: int | None = None) -> str:
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        args = parts[1:]
        if cmd in ("/start", "/help"):
            return HELP
        if cmd == "/add":
            if not args:
                return "Usage: /add &lt;TRON_ADDRESS&gt; [label]"
            return (await self.admin.add_wallet(args[0], label=" ".join(args[1:]) or None, added_by=user_id)).message
        if cmd == "/remove":
            if not args:
                return "Usage: /remove &lt;TRON_ADDRESS&gt;"
            return (await self.admin.remove_wallet(args[0])).message
        if cmd == "/pause":
            if args:
                return (await self.admin.set_wallet_status(args[0], WalletStatus.PAUSED)).message
            return (await self.admin.set_global_pause(True)).message
        if cmd == "/resume":
            if args:
                return (await self.admin.set_wallet_status(args[0], WalletStatus.ACTIVE)).message
            res = await self.admin.set_global_pause(False)
            self.notifier.wake()
            return res.message
        if cmd == "/list":
            return await self.list_text()
        if cmd == "/status":
            return await self.status_provider()
        if cmd == "/cases":
            return await self.cases_text()
        if cmd == "/case":
            if not args:
                return "Usage: /case &lt;CASE_ID&gt;"
            return await self.case_text(args[0])
        if cmd == "/recipients":
            if not args:
                return "Usage: /recipients &lt;ADDRESS&gt;"
            return await self.recipients_text(args[0])
        return "Unknown command. /help"

    async def list_text(self) -> str:
        wallets = await self.admin.list_wallets()
        if not wallets:
            return "No wallets monitored yet. Use /add &lt;ADDRESS&gt;."
        lines = [f"<b>Monitored wallets ({len(wallets)})</b>", ""]
        async with self.sf() as s:
            counts = dict((await s.execute(select(HistoricalRecipient.victim_wallet, func.count()).group_by(HistoricalRecipient.victim_wallet))).all())
        for w in wallets[:100]:
            icon = "🟢" if w.status == WalletStatus.ACTIVE.value else "⏸"
            hist = {
                "COMPLETE": "history ✓",
                "RUNNING": f"history scanning ({w.history_transfers_scanned})",
                "PENDING": "history queued",
                "FAILED": "history failed",
            }[w.history_status]
            label = f" — {html.escape(w.label)}" if w.label else ""
            lines.append(f"{icon} <code>{w.address}</code>{label}\n    {hist} · {counts.get(w.address, 0)} recipients")
        if len(wallets) > 100:
            lines.append(f"… and {len(wallets) - 100} more")
        if self.admin.alerts_paused:
            lines.append("\n⏸ <b>Alert delivery is globally paused</b> (/resume)")
        return "\n".join(lines)

    async def cases_text(self, limit: int = 10) -> str:
        async with self.sf() as s:
            rows = (await s.execute(select(PoisoningEvent).order_by(PoisoningEvent.id.desc()).limit(limit))).scalars().all()
        if not rows:
            return "No incidents recorded."
        icon = {EventType.SUCCESSFUL_POISONING_EVENT.value: "🚨", EventType.POISONING_CANDIDATE.value: "🟠", EventType.POISONING_ATTEMPT.value: "🟡"}
        lines = ["<b>Recent incidents</b>", ""]
        for e in rows:
            lines.append(
                f"{icon[e.event_type]} <code>{e.case_id}</code> {format_amount(e.amount, e.token_decimals)} {e.token_symbol} "
                f"{short(e.victim_wallet)} → {short(e.suspicious_recipient)} · {e.confidence}/100{' · historical' if e.is_historical else ''}"
            )
        return "\n".join(lines)

    async def case_text(self, case: str) -> str:
        async with self.sf() as s:
            ev = await repo.event_by_case(s, case)
            if ev is None:
                return "Case not found."
            b = await rs.load_bundle(s, ev.id)
        return rs.telegram_alert(b)

    async def recipients_text(self, address: str) -> str:
        try:
            addr = normalize_address(address)
        except InvalidAddress:
            return "❌ Invalid TRON address."
        token = self.s.primary_token
        async with self.sf() as s:
            rows = await repo.top_recipients(s, addr, token.contract, limit=15)
        if not rows:
            return "No recipients recorded for this wallet."
        lines = [f"<b>Top {token.symbol} recipients</b> of <code>{short(addr, 8, 6)}</code>", ""]
        for r in rows:
            flag = " ⚠️ flagged" if r.flagged_suspicious else ""
            lines.append(
                f"<code>{r.recipient_wallet}</code>{flag}\n    {r.transaction_count}× · total {format_amount(r.total_amount, token.decimals)} · "
                f"avg {format_amount(r.average_amount, token.decimals)} · largest {format_amount(r.largest_amount, token.decimals)}\n"
                f"    first {iso(r.first_seen)} · last {iso(r.last_seen)}"
            )
        return "\n".join(lines)

    # ----------------------------------------------------------------- buttons
    async def handle_callback(self, cb: dict) -> None:
        user = cb.get("from") or {}
        msg = cb.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        admin = self.is_admin(user.get("id"), chat_id)
        await self._record_user(user, admin)
        if not admin:
            await self.t.answer_callback(cb.get("id", ""), "Not authorized")
            log.warning("TELEGRAM_UNAUTHORIZED_CALLBACK", user_id=user.get("id"))
            return
        data = str(cb.get("data") or "")
        action, _, ref = data.partition(":")
        if not ref.isdigit():
            await self.t.answer_callback(cb.get("id", ""), "Invalid action")
            return
        await self.t.answer_callback(cb.get("id", ""), "Working…")
        handler = {
            "copy": self.cb_copy,
            "trace": self.cb_trace,
            "report": self.cb_report,
            "xprep": self.cb_xprep,
            "xpost": self.cb_xpost,
            "xcancel": self.cb_xcancel,
        }.get(action)
        if handler:
            await handler(chat_id, int(ref))

    async def _bundle(self, event_id: int) -> rs.CaseBundle | None:
        async with self.sf() as s:
            try:
                return await rs.load_bundle(s, event_id)
            except KeyError:
                return None

    async def cb_copy(self, chat_id: int, event_id: int) -> None:
        b = await self._bundle(event_id)
        if b is None:
            await self.reply(chat_id, "Case not found.")
            return
        packet = rs.evidence_packet(b)
        if len(packet) > 3500:
            await self.t.send_document(chat_id, f"{b.event.case_id}-evidence.txt", packet.encode(), caption=f"Evidence packet {b.event.case_id}")
        else:
            await self.reply(chat_id, f"<pre>{html.escape(packet)}</pre>")

    async def cb_trace(self, chat_id: int, event_id: int) -> None:
        async with self.sf() as s, s.begin():
            ev = await s.get(PoisoningEvent, event_id)
            if ev is None:
                await self.reply(chat_id, "Case not found.")
                return
            last = (await s.execute(select(func.max(FundTrace.trace_run)).where(FundTrace.event_id == event_id))).scalar_one() or 0
            run = last + 1
            await repo.enqueue_job(s, "TRACE", f"{event_id}:{run}:manual", self.clock.now(), {"event_id": event_id, "run": run, "manual": True})
            ev.trace_status = "PENDING"
        self.jobs_wakeup.set()
        await self.reply(chat_id, f"🔎 Tracing funds for <code>{ev.case_id}</code> (run {run}, up to {self.s.trace_hops} hops)…")

    async def cb_report(self, chat_id: int, event_id: int) -> None:
        b = await self._bundle(event_id)
        if b is None:
            await self.reply(chat_id, "Case not found.")
            return
        md = rs.investigator_report(b)
        js = rs.report_json_text(b)
        save_reports(self.s.output_dir, b, md, js)
        await self.t.send_document(chat_id, f"{b.event.case_id}-report.md", md.encode(), caption=f"Investigator report {b.event.case_id}")
        await self.t.send_document(chat_id, f"{b.event.case_id}.json", js.encode(), caption="Machine-readable case file")

    async def cb_xprep(self, chat_id: int, event_id: int) -> None:
        b = await self._bundle(event_id)
        if b is None:
            await self.reply(chat_id, "Case not found.")
            return
        text = rs.x_post(b)
        now = self.clock.now()
        async with self.sf() as s, s.begin():
            d = XPostDraft(event_id=event_id, text=text, status="DRAFT", created_at=now, updated_at=now)
            s.add(d)
            await s.flush()
            draft_id = d.id
        buttons = []
        if self.x.enabled:
            buttons.append({"text": "📤 POST TO X", "callback_data": f"xpost:{draft_id}"})
        buttons.append({"text": "CANCEL", "callback_data": f"xcancel:{draft_id}"})
        note = (
            "Review carefully. Nothing is posted unless you press POST TO X."
            if self.x.enabled
            else "X posting is disabled (X_ENABLED=false) — copy the text manually."
        )
        await self.reply(
            chat_id,
            f"<b>🐦 X post draft</b> — <code>{b.event.case_id}</code>\n\n<pre>{html.escape(text)}</pre>\n\n<i>{note}</i>",
            {"inline_keyboard": [buttons]},
        )

    async def cb_xpost(self, chat_id: int, draft_id: int) -> None:
        async with self.sf() as s, s.begin():
            d = await s.get(XPostDraft, draft_id)
            if d is None or d.status != "DRAFT":
                await self.reply(chat_id, "Draft not found or already handled.")
                return
            d.status = "POSTING"
            text = d.text
        try:
            validate_post(text)
            tweet_id = await self.x.post(text)
            status, err, msg = "POSTED", None, f"✅ Posted to X (id {tweet_id})."
        except XError as exc:
            tweet_id, status, err, msg = None, "FAILED", str(exc), f"❌ Not posted: {html.escape(str(exc))}"
        async with self.sf() as s, s.begin():
            d = await s.get(XPostDraft, draft_id)
            d.status, d.tweet_id, d.error, d.updated_at = status, tweet_id, err, self.clock.now()
        await self.reply(chat_id, msg)

    async def cb_xcancel(self, chat_id: int, draft_id: int) -> None:
        async with self.sf() as s, s.begin():
            d = await s.get(XPostDraft, draft_id)
            if d is not None and d.status == "DRAFT":
                d.status, d.updated_at = "CANCELLED", self.clock.now()
        await self.reply(chat_id, "✖️ X post cancelled.")


def split_message(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            out.append(cur)
            cur = ""
        cur += (("\n" if cur else "") + line)[:limit]
    if cur:
        out.append(cur)
    if any(t.count("<pre>") != t.count("</pre>") for t in out):
        return [html.unescape(_TAG.sub("", t)) for t in out]
    return out


def save_reports(output_dir: str, b: rs.CaseBundle, md: str, js: str) -> Path:
    d = Path(output_dir) / "cases" / b.event.case_id
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / "report.md").write_text(md)
        (d / "report.json").write_text(js)
        (d / "evidence_packet.txt").write_text(rs.evidence_packet(b))
        (d / "x_post.txt").write_text(rs.x_post(b))
    except OSError as exc:
        log.warning("REPORT_SAVE_FAILED", error=str(exc)[:100])
    return d
