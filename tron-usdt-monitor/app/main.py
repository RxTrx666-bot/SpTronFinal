"""Entrypoint: ``python -m app.main``."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import Awaitable, Callable

from dotenv import find_dotenv, load_dotenv

from app import __version__
from app.config import ConfigError, Settings
from app.database import create_repository
from app.filters import TransferFilter
from app.formatting import build_startup_notice
from app.logger import kv, setup_logging
from app.startup_checks import StartupCheckError, run_startup_checks
from app.stats import MonitorStats
from app.telegram_bot import START_BUTTON, AlertDispatcher, TelegramBot, TelegramClient
from app.timeutil import now_ms
from app.tron_client import TronClient
from app.tron_monitor import TransactionProcessor, build_monitor
from app.tx_limit import TxLimitTracker

log = logging.getLogger("app")


def make_heartbeat(path: str) -> Callable[[], None]:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    def beat() -> None:
        try:
            target.write_text(str(now_ms()))
        except OSError:
            pass

    return beat


async def supervise(name: str, factory: Callable[[], Awaitable[None]], stop: asyncio.Event) -> None:
    """Restart a long-running task if it ever crashes unexpectedly."""
    backoff = 1.0
    while not stop.is_set():
        try:
            await factory()
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("task_crashed_restarting", extra=kv(task=name, restart_in_s=backoff))
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


async def run(settings: Settings) -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover (Windows)
            pass

    repo = create_repository(settings.database_url)
    await repo.init()
    tron = TronClient(
        settings.tron_api_url,
        api_key=settings.tron_api_key,
        api_key_header=settings.tron_api_key_header,
        timeout=settings.tron_request_timeout,
        max_retries=settings.tron_max_retries,
    )
    telegram = TelegramClient(settings.telegram_bot_token, api_base=settings.telegram_api_url)
    stats = MonitorStats()
    dispatcher = AlertDispatcher(settings, telegram, repo, stats)
    limit_tracker = TxLimitTracker(repo, settings.tx_limit_threshold, dispatcher, settings.pause_on_limit)
    dispatcher.limit_tracker = limit_tracker
    bot = TelegramBot(settings, telegram, repo, stats, tron, limit_tracker)
    processor = TransactionProcessor(
        settings,
        TransferFilter(
            settings.wallet_address,
            settings.usdt_contract,
            settings.min_raw,
            settings.max_raw,
            settings.token_symbol,
            settings.token_decimals,
            settings.alert_directions,
        ),
        repo,
        dispatcher,
        stats,
        limit_tracker=limit_tracker,
    )
    monitor = build_monitor(settings, tron, repo, processor, stats, heartbeat=make_heartbeat(settings.heartbeat_file))

    exit_code = 0
    try:
        log.info(
            "tron_api_connecting",
            extra=kv(api_host=settings.tron_api_host, api_key_set=bool(settings.tron_api_key)),
        )
        try:
            stats.warnings = await run_startup_checks(settings, tron)
        except StartupCheckError as exc:
            log.critical("startup_check_failed", extra=kv(error=str(exc)))
            await bot.notify_admin(f"❌ Monitor NOT started:\n{exc}")
            return 2

        await limit_tracker.load()
        if settings.start_paused:
            await limit_tracker.pause("startup (START_PAUSED=true)")
        stats.paused = limit_tracker.paused
        await dispatcher.load_pending()
        await limit_tracker.check()  # re-send the notice if a crash happened before delivery
        if settings.notify_on_startup:
            await bot.notify_admin(
                build_startup_notice(settings, stats.warnings, limit_tracker.paused),
                reply_markup=START_BUTTON if limit_tracker.paused else None,
            )

        tasks = [
            asyncio.create_task(supervise("alerts", lambda: dispatcher.run(stop), stop), name="alerts"),
            asyncio.create_task(supervise("telegram", lambda: bot.run(stop), stop), name="telegram"),
            asyncio.create_task(supervise("monitor", lambda: monitor.run(stop), stop), name="monitor"),
        ]
        await stop.wait()
        log.info("shutdown_requested")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await tron.close()
        await telegram.close()
        await repo.close()
        log.info("shutdown_complete")
    return exit_code


def main() -> None:
    load_dotenv(find_dotenv(usecwd=True))
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(2)
    setup_logging(settings.log_level, settings.log_format, settings.secrets())
    log.info(
        "bot_starting",
        extra=kv(
            version=__version__,
            wallet=settings.wallet_address,
            contract=settings.usdt_contract,
            range=f"{settings.min_usdt}-{settings.max_usdt} USDT",
            mode=settings.monitor_mode,
            poll_interval_s=settings.poll_interval_seconds,
            confirmed_only=settings.confirmed_only,
            backfill=settings.backfill_enabled,
        ),
    )
    sys.exit(asyncio.run(run(settings)))


if __name__ == "__main__":
    main()
