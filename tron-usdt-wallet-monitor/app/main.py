"""Entry point: ``python -m app.main``.

Wires the pipeline:

    TRON API client -> stream / scheduler (fetch) -> normalizer -> processor
    (DB, discovery, detection) -> alerts table -> dispatcher -> Telegram
"""

from __future__ import annotations

import asyncio
import signal
import time

from app.alerts.dispatcher import AlertDispatcher
from app.amounts import fmt_usdt
from app.alerts.formatter import code
from app.config import OFFICIAL_USDT_CONTRACT, Settings
from app.db import repository as repo
from app.db.session import create_engine, create_session_factory, run_migrations, wait_for_database
from app.domain import dt_to_ms, ms_to_dt, utcnow
from app.engine.processor import TransferProcessor
from app.engine.registry import WalletRegistry
from app.health import serve_health, watch_database
from app.logging_setup import configure_logging, get_logger, register_secret
from app.monitor.scheduler import WalletScheduler
from app.monitor.stream import StreamMonitor
from app.runtime import STATE_MONITOR_STARTED, STATE_PAUSED, Runtime
from app.telegram.client import TelegramClient, TelegramError
from app.telegram.commands import CommandHandler
from app.tron.client import TronGridClient, TronSource

log = get_logger("app.main")


async def build_runtime(settings: Settings, session_factory, source: TronSource, *, now_ms: int | None = None) -> Runtime:
    """Load (or create) persistent state and construct the monitoring components."""
    now_ms = now_ms if now_ms is not None else dt_to_ms(utcnow())
    started_key = f"{STATE_MONITOR_STARTED}:{settings.root_wallet}"
    async with session_factory() as s, s.begin():
        await repo.ensure_root_wallet(s, settings.root_wallet)
        started = await repo.get_state(s, started_key)
        if started is None:
            started = str(now_ms)
            await repo.set_state(s, started_key, started)
        paused = await repo.get_state(s, STATE_PAUSED) == "1"
        wallets = await repo.load_wallets(s)

    registry = WalletRegistry(settings.root_wallet, settings.max_hops)
    registry.load(wallets)
    processor = TransferProcessor(settings, session_factory, registry, monitor_started_ms=int(started))
    processor.paused = paused
    stream = StreamMonitor(settings, source, processor, session_factory)
    scheduler = WalletScheduler(settings, source, processor, session_factory, registry)
    processor.on_discovered = scheduler.enqueue_discovered
    await stream.initialise(start_ms=int(started))
    log.info(
        "State loaded",
        root=settings.root_wallet,
        monitored=len(registry.monitored()),
        monitoring_since=ms_to_dt(int(started)).isoformat(),
        paused=paused,
    )
    return Runtime(settings=settings, session_factory=session_factory, registry=registry, processor=processor, stream=stream, scheduler=scheduler)


async def verify_token(rt: Runtime, tron: TronGridClient) -> list[str]:
    """Read symbol()/decimals() from the configured contract; return warnings."""
    s = rt.settings
    warnings: list[str] = []
    if not s.is_official_usdt:
        warnings.append(f"USDT_CONTRACT {s.usdt_contract} is not the official Tether USDT contract ({OFFICIAL_USDT_CONTRACT}).")
    try:
        info = await tron.get_token_info()
        rt.token_info = info
        log.info("Token contract verified", contract=s.usdt_contract, symbol=info.get("symbol"), decimals=info.get("decimals"))
        if info.get("decimals") is not None and int(info["decimals"]) != s.usdt_decimals:
            warnings.append(f"Contract reports decimals={info['decimals']} but USDT_DECIMALS={s.usdt_decimals}.")
        if info.get("symbol") and info["symbol"] != "USDT":
            warnings.append(f"Contract symbol is {info['symbol']!r}, not 'USDT'.")
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not verify token contract", error=str(exc)[:150])
    for w in warnings:
        log.warning("CONFIGURATION WARNING", detail=w)
    return warnings


async def run(settings: Settings) -> None:
    for secret in settings.secrets():
        register_secret(secret)
    log.info(
        "Starting TRON USDT wallet monitor (read-only)",
        root=settings.root_wallet,
        contract=settings.usdt_contract,
        threshold=f">= {fmt_usdt(settings.alert_min_base_units)} USDT",
        poll=f"{settings.poll_interval_seconds}s",
        max_hops=settings.max_hops,
        backfill=f"{settings.backfill_lookback_days}d" if settings.backfill_enabled else "off",
    )
    engine = create_engine(settings.database_url, settings.db_pool_size)
    await wait_for_database(engine)
    if settings.db_auto_migrate:
        log.info("Applying database migrations")
        await asyncio.to_thread(run_migrations, settings.database_url)
    sf = create_session_factory(engine)

    tron = TronGridClient(settings)
    tg = TelegramClient(
        settings.telegram_bot_token.get_secret_value(),
        api_url=settings.telegram_api_url,
        min_interval=settings.telegram_min_send_interval_seconds,
    )
    rt = await build_runtime(settings, sf, tron)
    rt.tron = tron
    dispatcher = AlertDispatcher(settings, sf, tg)
    rt.dispatcher = dispatcher
    rt.processor.on_alert = dispatcher.wake

    warnings = await verify_token(rt, tron)
    if settings.send_startup_message:
        await _startup_message(rt, tg, warnings)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover
            pass

    tasks = [
        asyncio.create_task(rt.stream.run(stop), name="stream"),
        asyncio.create_task(dispatcher.run(stop), name="dispatcher"),
        asyncio.create_task(serve_health(rt, stop), name="health"),
        asyncio.create_task(watch_database(rt, engine, stop), name="db-watch"),
    ]
    if settings.reconcile_enabled:
        tasks.append(asyncio.create_task(rt.scheduler.run(stop), name="scheduler"))
    if settings.backfill_enabled:
        tasks.append(asyncio.create_task(rt.scheduler.run_backfill(stop), name="backfill"))
    if settings.telegram_commands_enabled:
        tasks.append(asyncio.create_task(CommandHandler(rt, tg).run(stop), name="telegram-commands"))

    await stop.wait()
    log.info("Shutting down")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await tron.close()
    await tg.close()
    await engine.dispose()
    log.info("Stopped", uptime=f"{time.time() - rt.started_at:.0f}s")


async def _startup_message(rt: Runtime, tg: TelegramClient, warnings: list[str]) -> None:
    s = rt.settings
    lines = [
        "✅ <b>TRON USDT Wallet Monitor started</b>",
        "",
        f"Root wallet: {code(s.root_wallet)}",
        f"Monitored wallets: {len(rt.registry.monitored()):,}",
        f"Alert threshold: ≥ {fmt_usdt(s.alert_min_base_units)} USDT",
        f"Poll interval: {s.poll_interval_seconds}s · Backfill: {'on' if s.backfill_enabled else 'off'}",
        f"Alerts: {'⏸ PAUSED' if rt.paused else 'active'}",
    ]
    for w in warnings:
        lines += ["", f"⚠️ {w}"]
    try:
        await tg.send_message(s.telegram_admin_chat_id, "\n".join(lines))
    except TelegramError as exc:
        log.error("Startup message failed (check TELEGRAM_BOT_TOKEN / TELEGRAM_ADMIN_CHAT_ID)", error=str(exc)[:150])


def main() -> None:
    settings = Settings()
    configure_logging(settings.log_level, settings.log_format)
    for secret in settings.secrets():
        register_secret(secret)
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
