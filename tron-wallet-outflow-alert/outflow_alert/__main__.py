"""Entry point: ``python -m outflow_alert`` (run) or ``python -m outflow_alert check`` (startup checks only)."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from outflow_alert.config import ConfigError, load_settings
from outflow_alert.monitor import Monitor, StateMismatch
from outflow_alert.storage import Storage
from outflow_alert.telegram import SendError, TelegramClient
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
        log.info("cursor=%s pending=%s outbox=%s", storage.get_cursor(), storage.pending_count(), storage.outbox_count())
        await monitor.run()
        log.info("stopped")
        return 0
    finally:
        await tron.close()
        await telegram.close()
        if storage is not None:
            storage.close()


def main() -> None:
    check_only = len(sys.argv) > 1 and sys.argv[1] == "check"
    sys.exit(asyncio.run(amain(check_only)))


if __name__ == "__main__":
    main()
