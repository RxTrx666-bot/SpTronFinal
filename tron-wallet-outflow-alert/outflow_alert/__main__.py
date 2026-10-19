"""Entry point.

  python -m outflow_alert                     run the bot
  python -m outflow_alert check               startup checks only
  python -m outflow_alert alerts [N]          list the last N alerts (default 20)
  python -m outflow_alert delete <txid|last> [CHAT_ID]
                                              delete an alert from every chat, or only from CHAT_ID
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from outflow_alert.commands import CommandHandler
from outflow_alert.amounts import format_units
from outflow_alert.config import ConfigError, load_settings
from outflow_alert.deletion import delete_alert, format_results
from outflow_alert.monitor import Monitor, StateMismatch
from outflow_alert.storage import Storage
from outflow_alert.telegram import SendError, TelegramClient, format_time
from outflow_alert.tron_client import TronClient, TronError
from outflow_alert.verifier import TokenCheckError, verify_token

log = logging.getLogger("outflow_alert")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    # httpx logs request URLs at INFO; the Telegram URL contains the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


async def startup_checks(settings, tron: TronClient, telegram: TelegramClient) -> None:
    for attempt in range(1, 6):
        try:
            await verify_token(tron, settings.usdt_contract, settings.wallet, symbol="USDT",
                               decimals=settings.token_decimals)
            break
        except TronError as exc:
            if not exc.retryable or attempt == 5:
                raise
            log.warning("token check attempt %s failed: %s; retrying", attempt, exc)
            await asyncio.sleep(2 * attempt)
    log.info("token verified: %s symbol()=USDT decimals()=%s", settings.usdt_contract, settings.token_decimals)
    me = await telegram.get_me()
    log.info("telegram bot @%s ready, %s chat(s)", (me or {}).get("username", "?"), len(settings.telegram_chat_ids))


async def amain(check_only: bool) -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    setup_logging(settings.log_level)
    log.info("starting read-only monitor: %s", settings.describe())

    tron = TronClient(settings.trongrid_base_url, settings.tron_api_key, timeout=settings.http_timeout_seconds)
    telegram = TelegramClient(settings.telegram_bot_token)
    storage = None
    try:
        try:
            await startup_checks(settings, tron, telegram)
        except (TokenCheckError, TronError, SendError) as exc:
            log.error("startup check failed: %s", exc)
            return 3
        if check_only:
            return 0
        storage = Storage(settings.db_path)
        monitor = Monitor(settings, storage, tron, telegram)
        try:
            monitor.bind_state()
        except StateMismatch as exc:
            log.error("%s", exc)
            return 4
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, monitor.stop)
            except NotImplementedError:  # pragma: no cover - Windows
                pass
        log.info("cursor=%s pending=%s outbox=%s paused=%s", storage.get_cursor(), storage.pending_count(),
                 storage.outbox_count(), storage.is_paused())
        await monitor.run(CommandHandler(monitor, telegram).run())
        log.info("stopped")
        return 0
    finally:
        await tron.close()
        await telegram.close()
        if storage is not None:
            storage.close()


async def list_alerts(settings, limit: int) -> int:
    storage = Storage(settings.db_path)
    try:
        alerts = storage.recent_alerts(limit)
        if not alerts:
            print("no alerts yet")
        for a in alerts:
            chats = ", ".join(a.chats) or "-"
            print(f"{format_time(a.block_ts)}  {format_units(a.amount_raw):>10} USDT  -> {a.to_address}\n"
                  f"    txid {a.txid}\n    in chats: {chats}")
        return 0
    finally:
        storage.close()


async def delete_command(settings, target: str, chat_id: str | None) -> int:
    storage = Storage(settings.db_path)
    telegram = TelegramClient(settings.telegram_bot_token)
    try:
        if target == "last":
            recent = storage.recent_alerts(1)
            if not recent:
                print("no alerts yet")
                return 1
            txid, log_index = recent[0].txid, recent[0].log_index
        else:
            matches = storage.resolve_txid(target)
            if len(matches) != 1:
                print("no alert with that txid" if not matches else f"{len(matches)} alerts match; use more characters")
                return 1
            txid, log_index = matches[0], None
        print(f"alert {txid}")
        results = await delete_alert(storage, telegram, txid, log_index, chat_id)
        print(format_results(results))
        return 0 if results and all(r.ok for r in results) else 1
    finally:
        await telegram.close()
        storage.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m outflow_alert")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("run", help="run the bot (default)")
    sub.add_parser("check", help="startup checks only")
    p_list = sub.add_parser("alerts", help="list recent alerts")
    p_list.add_argument("limit", nargs="?", type=int, default=20)
    p_del = sub.add_parser("delete", help="delete a sent alert from Telegram")
    p_del.add_argument("target", help="txid (or its first characters), or 'last'")
    p_del.add_argument("chat_id", nargs="?", help="only delete it from this chat")
    args = parser.parse_args()

    if args.cmd in ("alerts", "delete"):
        try:
            settings = load_settings()
        except ConfigError as exc:
            print(f"configuration error: {exc}", file=sys.stderr)
            sys.exit(2)
        setup_logging("WARNING")
        if args.cmd == "alerts":
            sys.exit(asyncio.run(list_alerts(settings, args.limit)))
        sys.exit(asyncio.run(delete_command(settings, args.target, args.chat_id)))
    sys.exit(asyncio.run(amain(args.cmd == "check")))


if __name__ == "__main__":
    main()
