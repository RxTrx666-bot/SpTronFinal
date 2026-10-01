"""Application entry point.

python -m app.main            run the monitor (default)
python -m app.main migrate    apply database migrations and exit
python -m app.main check      verify configuration + TRON API connectivity (read-only)
python -m app.main simulate   run the end-to-end simulation (no real blockchain access)
python -m app.main health     Docker healthcheck (exit 0 if the heartbeat is fresh)
"""

from __future__ import annotations

import asyncio
import signal
import sys
import time
from pathlib import Path

from sqlalchemy import func, select

from app import repository as repo
from app.config import OFFICIAL_USDT_TRC20, Settings, describe
from app.database import create_engine, create_session_factory, init_schema, wait_for_database
from app.domain import AlertStatus, EventType, HistoryStatus
from app.models import Alert, PoisoningEvent, Transaction, WatchedWallet
from app.services.admin_service import AdminService
from app.services.fund_tracer import FundTracer
from app.services.history_service import HistoryService
from app.services.investigator import Investigator
from app.services.labels import LabelService
from app.services.network_scanner import NetworkScanner
from app.services.notifier import Notifier
from app.services.poisoning_detector import LockManager, PoisoningDetector, WalletRegistry
from app.services.telegram_service import BotApiTransport, ConsoleTransport, TelegramBot
from app.services.transaction_monitor import AccountMonitor, BlockMonitor, Ingestor
from app.services.tron_service import TronGridClient
from app.services.x_service import XService
from app.utils.clock import Clock, SystemClock, from_ms, iso
from app.utils.logging import get_logger, register_secrets, setup_logging
from app.workers.alert_worker import AlertWorker
from app.workers.job_worker import JobWorker
from app.workers.maintenance import ConfirmationWorker, Heartbeat, NetworkPruneWorker, RecoveryWorker, SystemLogWriter

log = get_logger(__name__)


class Application:
    def __init__(self, settings: Settings, *, source=None, transport=None, clock: Clock | None = None) -> None:
        self.s = settings
        register_secrets(settings.secrets())
        self.clock = clock or SystemClock()
        self.engine = create_engine(settings.database_url, settings.database_pool_size)
        self.sf = create_session_factory(self.engine)
        if source is None:
            if settings.simulation_mode:
                from app.simulation.chain import SimulatedChain

                source = SimulatedChain()
            else:
                source = TronGridClient(settings)
        self.source = source
        if transport is None:
            transport = BotApiTransport(settings.telegram_bot_token, settings.telegram_timeout_seconds) if settings.telegram_bot_token else ConsoleTransport()
        self.transport = transport

        self.registry = WalletRegistry()
        self.locks = LockManager()
        self.notifier = Notifier(settings, self.clock)
        self.syslog = SystemLogWriter(self.sf, self.clock)
        self.detector = PoisoningDetector(settings, self.sf, self.clock, self.registry, self.notifier, self.locks)
        self.jobs_wakeup = self.detector.jobs_wakeup
        self.labels = LabelService(settings, self.sf, source, self.clock)
        self.investigator = Investigator(settings, self.sf, source, self.clock, self.detector, self.notifier, self.labels)
        self.tracer = FundTracer(settings, self.sf, source, self.clock, self.labels)
        self.history = HistoryService(settings, self.sf, source, self.clock, self.registry, self.detector, self.notifier, self.locks)
        self.ingestor = Ingestor(settings, self.sf, source, self.clock, self.registry, self.detector)
        self.admin = AdminService(settings, self.sf, self.clock, self.registry, self.jobs_wakeup, audit=self.syslog.audit)
        self.x = XService(settings)
        self.network = NetworkScanner(settings, self.sf, self.clock, self.registry, self.detector) if settings.network_wide else None
        if settings.monitor_mode == "account":
            self.monitor = AccountMonitor(settings, self.sf, source, self.clock, self.ingestor)
        else:
            self.monitor = BlockMonitor(settings, self.sf, source, self.clock, self.ingestor, self.network)
        self.alerts = AlertWorker(settings, self.sf, self.clock, transport, self.notifier, self.admin)
        self.jobs = JobWorker(
            settings, self.sf, self.clock,
            history=self.history, investigator=self.investigator, tracer=self.tracer, notifier=self.notifier, wakeup=self.jobs_wakeup,
        )  # fmt: skip
        self.confirmations = ConfirmationWorker(settings, self.sf, source, self.clock, self.notifier)
        self.recovery = RecoveryWorker(settings, self.ingestor)
        self.bot = TelegramBot(settings, self.sf, self.clock, transport, self.admin, self.notifier, self.x, self.status_text, self.jobs_wakeup)
        self.stop_event = asyncio.Event()
        self.tasks: list[asyncio.Task] = []
        self.started_at = time.time()

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        await wait_for_database(self.engine)
        await init_schema(self.engine, self.s.database_url, self.s.auto_migrate)
        await self.registry.refresh(self.sf)
        await self.admin.load()
        async with self.sf() as s, s.begin():
            reset = await repo.reset_running_jobs(s, self.clock.now())
        if reset:
            log.info("JOBS_RECOVERED", count=reset)
        self.syslog.install()
        log.info("STARTED", **describe(self.s), wallets=len(self.registry.wallets))

    async def run(self) -> None:
        await self.start()
        recovered = await self.ingestor.recover_pending()
        if recovered:
            log.info("STARTUP_RECOVERY", pending_analysed=recovered)
        if self.s.send_startup_message:
            async with self.sf() as s, s.begin():
                await self.notifier.text_alert(
                    s, f"startup:{int(time.time())}",
                    f"🟢 <b>Poisoning monitor started</b>\nMode: {describe(self.s)['mode']} · wallets: {len(self.registry.wallets)}",
                )  # fmt: skip
        coros = [
            self.monitor.run(self.stop_event),
            self.alerts.run(self.stop_event),
            self.jobs.run(self.stop_event),
            self.confirmations.run(self.stop_event),
            self.recovery.run(self.stop_event),
            self.syslog.run(self.stop_event),
            Heartbeat(self.s.heartbeat_file, self.monitor).run(self.stop_event),
        ]
        if self.network is not None and isinstance(self.monitor, BlockMonitor):
            coros.append(NetworkPruneWorker(self.s, self.network).run(self.stop_event))
        if getattr(self.transport, "polls", False):
            coros.append(self.bot.run(self.stop_event))
        self.tasks = [asyncio.create_task(c) for c in coros]
        await self.stop_event.wait()
        await self.close()

    def request_stop(self) -> None:
        self.stop_event.set()

    async def close(self) -> None:
        self.stop_event.set()
        for t in self.tasks:
            t.cancel()
        for t in list(self.jobs.tasks):
            t.cancel()
        await asyncio.gather(*self.tasks, *self.jobs.tasks, return_exceptions=True)
        await self.syslog.flush()
        self.syslog.uninstall()
        try:
            await self.source.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await self.transport.close()
        except Exception:  # noqa: BLE001
            pass
        await self.engine.dispose()

    # ------------------------------------------------------------------ status
    async def status_text(self) -> str:
        m = self.monitor
        async with self.sf() as s:
            wallets = dict((await s.execute(select(WatchedWallet.status, func.count()).group_by(WatchedWallet.status))).all())
            hist = dict(
                (
                    await s.execute(
                        select(WatchedWallet.history_status, func.count()).where(WatchedWallet.status != "REMOVED").group_by(WatchedWallet.history_status)
                    )
                ).all()
            )
            events = dict((await s.execute(select(PoisoningEvent.event_type, func.count()).group_by(PoisoningEvent.event_type))).all())
            pending_alerts = (await s.execute(select(func.count()).select_from(Alert).where(Alert.status == AlertStatus.PENDING.value))).scalar_one()
            pending_tx = (await s.execute(select(func.count()).select_from(Transaction).where(Transaction.analysis_status == "PENDING"))).scalar_one()
            lat = (
                (
                    await s.execute(
                        select(PoisoningEvent.alert_latency_ms).where(PoisoningEvent.alert_latency_ms.is_not(None)).order_by(PoisoningEvent.id.desc()).limit(50)
                    )
                )
                .scalars()
                .all()
            )
        lines = ["<b>📊 STATUS</b>", ""]
        lines.append(f"Mode: {describe(self.s)['mode']} · uptime {int((time.time() - self.started_at) // 60)} min")
        if isinstance(m, BlockMonitor):
            lag = (m.head - m.cursor) if (m.head is not None and m.cursor is not None) else "?"
            lines.append(f"Head block: {m.head} · processed: {m.cursor} · lag: {lag} block(s)")
            if m.last_block_latency_ms is not None:
                lines.append(f"Last block seen {m.last_block_latency_ms} ms after its timestamp")
            if m.last_block_ts_ms:
                lines.append(f"Last processed block time: {iso(from_ms(m.last_block_ts_ms))}")
        else:
            lines.append(f"Account polling rounds: {m.rounds}")
        ok = m.last_poll_ok and time.time() - m.last_poll_ok < 60
        lines.append(f"Monitor: {'🟢 healthy' if ok else '🔴 no successful poll in the last 60 s'}")
        if self.confirmations.solid_block:
            lines.append(f"Solidified block: {self.confirmations.solid_block}")
        api = getattr(self.source, "requests", None)
        if api is not None:
            per_day = int(api / max(time.time() - self.started_at, 1) * 86400)
            lines.append(f"TRON API usage: ≈{per_day:,} requests/day at the current rate")
            lines.append(
                f"TRON API requests: {api} · failures: {getattr(self.source, 'failures', 0)}"
                + (f" · last error: {self.source.last_error}" if getattr(self.source, "last_error", None) else "")
            )
        lines.append("")
        if self.network is not None and isinstance(m, BlockMonitor):
            n = self.network.stats
            lines.append(
                f"🌐 Network-wide detection: ON · {n['transfers']:,} USDT transfers scanned since start · "
                f"{n['lookalike_payments']} look-alike payments analysed · {n['dust_evidence']} poisoning dust transfers seen"
            )
            lines.append(f"Payment memory: {await self.network.remembered_pairs():,} sender→recipient pairs ({self.s.network_memory_days} days)")
        else:
            lines.append("🌐 Network-wide detection: OFF (only wallets added with /add)")
        lines.append(f"Watched wallets (/add): {wallets.get('ACTIVE', 0)} active · {wallets.get('PAUSED', 0)} paused")
        lines.append(
            f"History: {hist.get(HistoryStatus.COMPLETE.value, 0)} complete · {hist.get('RUNNING', 0)} running · {hist.get('PENDING', 0)} queued · {hist.get('FAILED', 0)} failed"
        )
        lines.append(
            f"Incidents: {events.get(EventType.SUCCESSFUL_POISONING_EVENT.value, 0)} successful · "
            f"{events.get(EventType.POISONING_CANDIDATE.value, 0)} candidates · {events.get(EventType.POISONING_ATTEMPT.value, 0)} attempts"
        )
        lines.append(f"Queued alerts: {pending_alerts} · transfers awaiting analysis: {pending_tx}")
        if lat:
            srt = sorted(lat)
            lines.append(f"Alert latency (detected→Telegram, last {len(srt)}): median {srt[len(srt) // 2]} ms · max {srt[-1]} ms")
        if self.admin.alerts_paused:
            lines.append("\n⏸ <b>Alert delivery is paused</b> (/resume)")
        return "\n".join(lines)


# ---------------------------------------------------------------------- CLI
async def _check(settings: Settings) -> int:
    client = TronGridClient(settings)
    try:
        print(f"API: {settings.tron_api_url} (key {'set' if settings.tron_api_key else 'NOT set'})")
        head = await client.get_now_block_number()
        solid = await client.get_solid_block_number()
        print(f"OK head block {head}, solidified {solid} (lag {head - solid})")
        for t in settings.token_list:
            print(
                f"Token {t.symbol}: {t.contract} decimals={t.decimals}"
                + ("" if t.contract == OFFICIAL_USDT_TRC20 or t.symbol != "USDT" else "  ⚠ NOT the official USDT TRC-20 contract")
            )
        blk = await client.get_block(head, {t.contract: t.decimals for t in settings.token_list})
        print(f"Block {head}: {len(blk.transfers)} transfers of configured tokens")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {type(exc).__name__}: {exc}")
        return 1
    finally:
        await client.close()


def _health(settings: Settings) -> int:
    p = Path(settings.heartbeat_file)
    try:
        age = time.time() - int(p.read_text().strip())
    except (OSError, ValueError):
        return 1
    return 0 if age < 180 else 1


async def _migrate(settings: Settings) -> None:
    engine = create_engine(settings.database_url)
    await wait_for_database(engine)
    await init_schema(engine, settings.database_url, True)
    await engine.dispose()
    print("migrations applied")


async def _run(settings: Settings) -> None:
    app = Application(settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, app.request_stop)
        except NotImplementedError:  # pragma: no cover
            pass
    await app.run()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else "run"
    settings = Settings()
    setup_logging(settings.log_level, settings.log_format)
    register_secrets(settings.secrets())
    if cmd == "run":
        if settings.simulation_mode:
            from app.simulation.runner import main as sim_main

            return sim_main([])
        asyncio.run(_run(settings))
        return 0
    if cmd == "migrate":
        asyncio.run(_migrate(settings))
        return 0
    if cmd == "check":
        return asyncio.run(_check(settings))
    if cmd == "simulate":
        from app.simulation.runner import main as sim_main

        return sim_main(argv[1:])
    if cmd == "health":
        return _health(settings)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
