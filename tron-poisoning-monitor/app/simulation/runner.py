"""End-to-end simulation (``python -m app.main simulate`` or ``SIMULATION_MODE=true``).

Scenario:
  1. Victim pays the legitimate recipient TLegit…Wr2c 12 times over 150 days.
  2. Five days ago the attacker sent 0.000001 USDT from the look-alike
     TLegit…Wr2c (different middle) to the victim and six other wallets.
  3. The wallet is added with the same code path as Telegram /add; the history
     scan and retrospective analysis run.
  4. Live: the victim sends 25,000 USDT to the look-alike.  The block monitor
     picks the block up, the detector raises SUCCESSFUL_POISONING_EVENT and the
     Telegram alert is delivered.
  5. The attacker forwards the funds over 4 hops to a (simulated) labelled
     exchange deposit address; investigation + fund trace run.
  6. The buttons COPY CASE / FULL REPORT / PREPARE X POST are pressed.

Outputs are written to ``<output>/simulation/``.  No real blockchain or
Telegram access happens unless TELEGRAM_BOT_TOKEN is set and --telegram is given.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from sqlalchemy import select

from app.config import Settings
from app.domain import EventType
from app.models import PoisoningEvent
from app.services import report_service as rs
from app.services.telegram_service import BotApiTransport, ConsoleTransport
from app.simulation import scenarios
from app.simulation.chain import SimulatedChain
from app.utils.logging import setup_logging

SIM_ADMIN = 424242


async def run_simulation(output_dir: str = "output", *, database_url: str | None = None, echo: bool = True, telegram: bool = False) -> dict:
    from app.main import Application

    out = Path(output_dir) / "simulation"
    out.mkdir(parents=True, exist_ok=True)
    db = out / "simulation.db"
    if database_url is None and db.exists():
        db.unlink()
    env = Settings()
    use_tg = telegram and bool(env.telegram_bot_token and env.admin_ids)
    settings = Settings(
        _env_file=None,
        simulation_mode=True,
        database_url=database_url or f"sqlite+aiosqlite:///{db}",
        telegram_bot_token=env.telegram_bot_token if use_tg else "",
        telegram_admin_chat_id=env.telegram_admin_chat_id if use_tg else str(SIM_ADMIN),
        send_startup_message=False,
        heartbeat_file=str(out / "heartbeat"),
        output_dir=str(out),
        trace_retrace_minutes=0,
        labels_file="",
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
    )
    chain = SimulatedChain()
    now_ms = int(time.time() * 1000)
    sc = scenarios.build_history(chain, now_ms - 60_000)
    transport = BotApiTransport(settings.telegram_bot_token) if use_tg else ConsoleTransport(echo=echo)
    app = Application(settings, source=chain, transport=transport)
    await app.start()
    try:
        print("\n=== 1. /add victim wallet (initial historical scan) ===")
        res = await app.admin.add_wallet(sc.victim, label="Simulated victim", added_by=SIM_ADMIN)
        print(res.message.replace("<code>", "").replace("</code>", ""))
        await app.jobs.drain()
        await app.alerts.deliver_due()

        print("\n=== 2. Live monitoring: victim sends 25,000 USDT to the look-alike ===")
        chain.mine(int(time.time() * 1000) - 6000)  # chain is live: latest block a few seconds old
        await app.monitor.step()  # establish block cursor at head
        attack_ts = int(time.time() * 1000)
        scenarios.attack(chain, sc, attack_ts)
        t0 = time.perf_counter()
        await app.monitor.step()
        t_analysed = time.perf_counter()
        await app.alerts.deliver_due()
        t_alert = time.perf_counter()

        async with app.sf() as s:
            ev = (
                await s.execute(select(PoisoningEvent).where(PoisoningEvent.tx_hash == sc.attack_tx, PoisoningEvent.victim_wallet == sc.victim))
            ).scalar_one_or_none()
        if ev is None or ev.event_type != EventType.SUCCESSFUL_POISONING_EVENT.value:
            raise SystemExit("SIMULATION FAILED: no SUCCESSFUL_POISONING_EVENT")

        print("\n=== 3. Attacker forwards funds; investigation + fund trace ===")
        scenarios.forward(chain, sc, attack_ts)
        for _ in range(25):  # let blocks solidify
            chain.mine()
        await app.monitor.step()
        await app.jobs.drain(timeout=60)
        await app.confirmations.run_once()
        await app.alerts.deliver_due()

        print("\n=== 4. Buttons: COPY CASE / FULL REPORT / PREPARE X POST ===")
        for action in ("copy", "report", "xprep"):
            await app.bot.handle_update(
                {"callback_query": {"id": action, "from": {"id": SIM_ADMIN}, "message": {"chat": {"id": SIM_ADMIN}}, "data": f"{action}:{ev.id}"}}
            )

        async with app.sf() as s:
            b = await rs.load_bundle(s, ev.id)
        files = {
            "telegram_alert.txt": _plain(rs.telegram_alert(b)),
            "evidence_packet.txt": rs.evidence_packet(b),
            "investigator_report.md": rs.investigator_report(b),
            "case.json": rs.report_json_text(b),
            "x_post.txt": rs.x_post(b),
            "fund_trace.txt": _plain(rs.trace_message(b)),
        }
        for name, content in files.items():
            (out / name).write_text(content)
        latency = {
            "block_to_detected_ms": b.event.chain_latency_ms,
            "detected_to_analysis_complete_ms": b.event.detection_latency_ms,
            "detected_to_telegram_ms": b.event.alert_latency_ms,
            "wall_clock_block_processing_ms": round((t_analysed - t0) * 1000, 1),
            "wall_clock_processing_to_alert_ms": round((t_alert - t0) * 1000, 1),
            "note": "Simulation: excludes network round-trips to the TRON API and Telegram.",
        }
        (out / "latency.json").write_text(json.dumps(latency, indent=2))
        summary = {
            "case_id": b.event.case_id,
            "event_type": b.event.event_type,
            "confidence": b.event.confidence,
            "similarity_pct": int(round(b.event.similarity_score * 100)),
            "poisoning_tx_observed": b.event.poisoning_tx_observed,
            "trace_hops": max((t.hop for t in b.traces), default=0),
            "latency": latency,
            "output_dir": str(out),
            "telegram_messages": len(getattr(transport, "messages", [])),
            "telegram_documents": len(getattr(transport, "documents", [])),
        }
        (out / "summary.json").write_text(json.dumps(summary, indent=2))
        print("\n=== SIMULATION RESULT ===")
        print(json.dumps(summary, indent=2))
        return summary
    finally:
        await app.close()


def _plain(text: str) -> str:
    import html
    import re

    return html.unescape(re.sub(r"<[^>]+>", "", text))


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="simulate")
    p.add_argument("--output", default=os.environ.get("OUTPUT_DIR", "output"))
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--telegram", action="store_true", help="also deliver to the real Telegram chat configured in .env")
    a = p.parse_args(argv)
    setup_logging(os.environ.get("LOG_LEVEL", "INFO"), "text")
    asyncio.run(run_simulation(a.output, echo=not a.quiet, telegram=a.telegram))
    return 0
