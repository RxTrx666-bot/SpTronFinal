"""Entry point.

    python -m app.main run         # 24/7 monitor (default)
    python -m app.main simulate    # offline simulation, proves the full flow
    python -m app.main migrate     # apply database migrations and exit
    python -m app.main check       # verify DB, TRON API, token contract, Telegram
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from collections.abc import Awaitable, Callable

from app.clock import Clock, SystemClock
from app.collector.tron_listener import EventSource, TronCollector, TronGridClient
from app.config.settings import Settings, get_settings
from app.database import repository as repo
from app.database.session import create_engine, create_session_factory, init_schema, wait_for_database
from app.domain import ALERT_PRIORITY, AlertType
from app.logging_setup import configure_logging, get_logger
from app.processing.analysis import AnalysisService
from app.processing.maintenance import Maintenance
from app.processing.pipeline import Pipeline, PipelineStats, TransactionMatcher
from app.telegram.alerts import AlertDispatcher, AlertSink, ConsoleSink
from app.telegram.bot import CommandHandler, TelegramClient, TelegramSink, run_command_loop
from app.telegram.messages import MessageFormatter
from app.watchlist.manager import WatchlistManager
from app.watchlist.model import WatchlistCache

log = get_logger("app")


class Application:
    """Wires every component together.  Tests and the simulator build it with a
    fake event source, an in-memory sink and SQLite."""

    def __init__(
        self,
        settings: Settings,
        *,
        source: EventSource | None = None,
        sink: AlertSink | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.settings = settings
        self.clock = clock or SystemClock()
        self.engine = create_engine(settings.database_url, settings.database_pool_size)
        self.session_factory = create_session_factory(self.engine)
        self.formatter = MessageFormatter(settings.display_timezone, settings.tronscan_tx_url)
        self.cache = WatchlistCache()
        self.manager = WatchlistManager(settings, self.formatter)
        self.telegram: TelegramClient | None = None
        if sink is None:
            if settings.telegram_enabled:
                self.telegram = TelegramClient(settings.telegram_bot_token, settings.telegram_timeout_seconds)
                sink = TelegramSink(self.telegram, settings.telegram_chat_ids)
            else:
                log.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set - alerts are printed to stdout")
                sink = ConsoleSink()
        self.dispatcher = AlertDispatcher(settings, self.session_factory, sink)
        self.stats = PipelineStats()
        self.analysis = AnalysisService(
            settings, self.session_factory, self.clock, self.cache, self.manager, self.dispatcher.wake
        )
        self.matcher = TransactionMatcher(
            settings,
            self.session_factory,
            self.clock,
            self.cache,
            self.manager,
            self.formatter,
            self.dispatcher.wake,
            on_followup=self.analysis.enqueue,
            stats=self.stats,
        )
        self.pipeline = Pipeline(
            settings, self.session_factory, self.clock, self.cache, self.matcher, self.analysis.enqueue, self.stats
        )
        self.source: EventSource = source or TronGridClient(settings)
        self.collector = TronCollector(settings, self.source, self.pipeline.process, self.session_factory, self.clock)
        self.maintenance = Maintenance(self)
        self.commands = CommandHandler(self)
        self.stop_event = asyncio.Event()

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        await wait_for_database(self.engine)
        await init_schema(self.engine, self.settings.database_url, self.settings.auto_migrate)
        async with self.session_factory() as s:
            self.cache.load(await repo.all_watchlist(s))
        await self.dispatcher.recover()
        await self.collector.initialise()
        log.info("Watchlist loaded", entries=len(self.cache))

    async def close(self) -> None:
        for closer in (getattr(self.source, "close", None), self.telegram.close if self.telegram else None):
            if closer:
                try:
                    await closer()
                except Exception:  # noqa: BLE001
                    pass
        await self.engine.dispose()

    async def on_backfill_complete(self) -> None:
        analysed = await self.analysis.run_backfill_analysis(self.stop_event)
        await self.send_system(
            f"BACKFILL:{self.collector.stats.backfill_end_ms}",
            self.formatter.backfill_summary(
                days=self.settings.initial_history_days, analysed=analysed, snaps=self.cache.values()
            ),
        )

    async def send_system(self, key: str, text: str) -> None:
        async with self.session_factory() as s, s.begin():
            await repo.insert_alert(
                s,
                dedup_key=f"SYSTEM:{key}",
                alert_type=AlertType.SYSTEM.value,
                priority=ALERT_PRIORITY[AlertType.SYSTEM],
                message_text=text,
                created_at=self.clock.now(),
            )
        self.dispatcher.wake()

    async def verify_token_contract(self) -> None:
        if not self.settings.is_official_usdt_contract:
            log.warning(
                "USDT_CONTRACT_ADDRESS is not the official Tether USDT TRC-20 contract",
                configured=self.settings.usdt_contract_address,
                official="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
            )
        if isinstance(self.source, TronGridClient):
            try:
                info = await self.source.get_token_info()
                log.info("Token contract verified", symbol=info.get("symbol()"), decimals=info.get("decimals()"))
                if info.get("decimals()") not in (None, self.settings.usdt_decimals):
                    log.error("Token decimals mismatch", onchain=info.get("decimals()"), configured=self.settings.usdt_decimals)
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not verify token contract (continuing)", error=str(exc)[:200])

    async def run_forever(self) -> None:
        await self.start()
        await self.verify_token_contract()
        if self.settings.send_startup_message:
            await self.send_system(
                f"STARTUP:{int(self.clock.now().timestamp())}",
                f"🟢 <b>TRON USDT pattern monitor started</b>\nWatchlist entries: {len(self.cache)}\n"
                f"Min sequences: {self.settings.min_successful_sequences}, "
                f"min ratio: {self.settings.min_large_to_test_ratio}x, "
                f"min confidence: {self.settings.min_pattern_confidence.value}",
            )
        stop = self.stop_event
        tasks: list[tuple[str, Callable[[], Awaitable[None]]]] = [
            ("collector-confirmed", lambda: self.collector.run_confirmed(stop)),
            ("backfill", lambda: self.collector.run_backfill(stop, self.on_backfill_complete)),
            ("dispatcher", lambda: self.dispatcher.run(stop)),
            ("maintenance", lambda: self.maintenance.run(stop)),
        ]
        if self.settings.enable_unconfirmed:
            tasks.append(("collector-unconfirmed", lambda: self.collector.run_unconfirmed(stop)))
        for i in range(max(1, self.settings.analysis_workers)):
            tasks.append((f"analysis-{i}", lambda: self.analysis.run_worker(stop)))
        if self.telegram and self.settings.telegram_commands_enabled:
            tasks.append(
                ("telegram-commands", lambda: run_command_loop(self.telegram, self.settings.telegram_chat_ids, self.commands, stop))
            )
        running = [asyncio.create_task(supervise(name, fn, stop), name=name) for name, fn in tasks]
        await stop.wait()
        log.info("Shutting down")
        for t in running:
            t.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        await self.close()
        log.info("Shutdown complete")


async def supervise(name: str, factory: Callable[[], Awaitable[None]], stop: asyncio.Event) -> None:
    """Restart a crashed background task with backoff (one task never kills the app)."""
    delay = 1.0
    while not stop.is_set():
        try:
            await factory()
            return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Task crashed; restarting", task=name, retry_in=f"{delay:.0f}s")
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, 60)


# ---------------------------------------------------------------- CLI


async def _run(settings: Settings) -> None:
    app = Application(settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, app.stop_event.set)
        except NotImplementedError:  # pragma: no cover
            pass
    await app.run_forever()


async def _check(settings: Settings) -> int:
    ok = True
    app = Application(settings)
    try:
        await wait_for_database(app.engine, attempts=3)
        print("✔ database reachable")
    except Exception as exc:  # noqa: BLE001
        print(f"✘ database: {exc}")
        ok = False
    try:
        events, _ = await app.source.get_contract_events(only_confirmed=True, limit=1)
        print(f"✔ TRON API reachable ({len(events)} event)")
        info = await app.source.get_token_info()
        print(f"✔ token contract {settings.usdt_contract_address}: symbol={info.get('symbol()')} decimals={info.get('decimals()')}")
        if not settings.is_official_usdt_contract:
            print("⚠ this is NOT the official Tether USDT TRC-20 contract (TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t)")
    except Exception as exc:  # noqa: BLE001
        print(f"✘ TRON API: {exc}")
        ok = False
    if app.telegram:
        try:
            me = await app.telegram.get_me()
            print(f"✔ Telegram bot @{me.get('username')}")
            for chat in settings.telegram_chat_ids:
                try:
                    await app.telegram.send_message(chat, "✅ TRON USDT monitor: configuration check OK")
                    print(f"✔ test message sent to chat {chat}")
                except Exception as exc:  # noqa: BLE001
                    print(f"✘ Telegram chat {chat}: {exc}  (has this account pressed Start on the bot?)")
                    ok = False
        except Exception as exc:  # noqa: BLE001
            print(f"✘ Telegram: {exc}")
            ok = False
    else:
        print("⚠ Telegram not configured (alerts go to stdout)")
    await app.close()
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tron-usdt-monitor")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("run")
    sub.add_parser("migrate")
    sub.add_parser("check")
    sim = sub.add_parser("simulate")
    sim.add_argument("--telegram", action="store_true", help="also deliver simulated alerts to the configured Telegram chat")
    sim.add_argument("--database-url", default="sqlite+aiosqlite:///:memory:")
    args = parser.parse_args(argv)

    if args.cmd == "simulate":
        from app.simulation.runner import run_simulation

        configure_logging("INFO", "text")
        return asyncio.run(run_simulation(database_url=args.database_url, use_telegram=args.telegram))

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    if args.cmd == "migrate":
        from app.database.session import run_alembic_upgrade

        run_alembic_upgrade(settings.database_url)
        log.info("Migrations applied")
        return 0
    if args.cmd == "check":
        return asyncio.run(_check(settings))
    log.info(
        "Starting TRON USDT test-transfer pattern monitor (read-only)",
        contract=settings.usdt_contract_address,
        min_sequences=settings.min_successful_sequences,
        min_ratio=str(settings.min_large_to_test_ratio),
        min_confidence=settings.min_pattern_confidence.value,
    )
    asyncio.run(_run(settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
