"""Deleting alerts the bot already sent, from one chat or from every chat."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from outflow_alert.storage import Storage
from outflow_alert.telegram import SendError, TelegramClient

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeleteResult:
    chat_id: str
    ok: bool
    detail: str


async def delete_alert(
    db: Storage,
    tg: TelegramClient,
    txid: str,
    log_index: int | None = None,
    chat_id: str | None = None,
) -> list[DeleteResult]:
    """Delete an alert's Telegram messages (all chats, or only ``chat_id``) and cancel undelivered copies."""
    results: list[DeleteResult] = []
    for msg in db.sent_messages_for(txid, log_index):
        if chat_id is not None and msg.chat_id != str(chat_id):
            continue
        try:
            await tg.delete_message(msg.chat_id, msg.message_id)
        except SendError as exc:
            text = str(exc).lower()
            if "message to delete not found" in text:
                db.forget_sent_message(msg.chat_id, msg.message_id)
                results.append(DeleteResult(msg.chat_id, True, "already deleted"))
            elif "can't be deleted" in text:
                results.append(DeleteResult(msg.chat_id, False, "too old (Telegram allows deleting for 48 hours)"))
            else:
                results.append(DeleteResult(msg.chat_id, False, str(exc)))
            continue
        db.forget_sent_message(msg.chat_id, msg.message_id)
        results.append(DeleteResult(msg.chat_id, True, "deleted"))
        log.info("deleted alert %s from chat %s", txid, msg.chat_id)

    cancelled = db.drop_pending_outbox(txid, log_index, chat_id)
    if cancelled:
        results.append(DeleteResult(chat_id or "all", True, f"{cancelled} undelivered copy(ies) cancelled"))
    return results


def format_results(results: list[DeleteResult]) -> str:
    if not results:
        return "Nothing to delete: no sent copy of that alert is on record."
    return "\n".join(f"{'✅' if r.ok else '❌'} {r.chat_id}: {r.detail}" for r in results)
