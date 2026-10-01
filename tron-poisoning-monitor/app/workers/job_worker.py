"""Durable background jobs (table ``jobs``).

Job types:
  HISTORY      ref=<wallet>             initial history scan + retrospective analysis
  INVESTIGATE  ref=<event id>           on-chain evidence discovery and re-scoring
  TRACE        ref=<event id>:<run>[..] fund tracing (+ forwarding evidence, scheduled re-traces)

Jobs are claimed atomically, retried with backoff on API/DB failures, and
jobs left RUNNING by a crash are reset to PENDING at startup.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from app import repository as repo
from app.database import is_transient_db_error
from app.models import PoisoningEvent
from app.services.tron_service import TronApiError
from app.utils.clock import Clock
from app.utils.logging import get_logger

log = get_logger(__name__)

MAX_AUTO_TRACE_RUNS = 6


class JobWorker:
    def __init__(self, settings, session_factory, clock: Clock, *, history, investigator, tracer, notifier, wakeup: asyncio.Event) -> None:
        self.s = settings
        self.sf = session_factory
        self.clock = clock
        self.history = history
        self.investigator = investigator
        self.tracer = tracer
        self.notifier = notifier
        self.wakeup = wakeup
        self.limits = {"HISTORY": max(1, settings.history_concurrency), "INVESTIGATE": 3, "TRACE": 2}
        self.running: dict[str, int] = {k: 0 for k in self.limits}
        self.tasks: set[asyncio.Task] = set()
        self.completed = 0
        self.failed = 0

    async def claim_and_start(self) -> int:
        free = [t for t, lim in self.limits.items() if self.running[t] < lim]
        if not free:
            return 0
        async with self.sf() as s, s.begin():
            jobs = await repo.claim_due_jobs(s, self.clock.now(), free, limit=10)
            claimed = [(j.id, j.job_type, j.ref, dict(j.payload or {}), j.attempts) for j in jobs]
        started = 0
        for jid, jtype, ref, payload, attempts in claimed:
            if self.running[jtype] >= self.limits[jtype]:
                async with self.sf() as s, s.begin():  # give it back
                    await repo.finish_job(s, jid, self.clock.now(), error="deferred", retry_in=1, max_attempts=10_000)
                continue
            self.running[jtype] += 1
            task = asyncio.create_task(self._run(jid, jtype, ref, payload, attempts))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            started += 1
        return started

    async def _run(self, jid: int, jtype: str, ref: str, payload: dict, attempts: int) -> None:
        error = None
        try:
            if jtype == "HISTORY":
                await self.history.scan(ref)
            elif jtype == "INVESTIGATE":
                await self.investigate(int(ref))
            elif jtype == "TRACE":
                await self.trace(int(payload.get("event_id") or ref.split(":")[0]), int(payload.get("run", 1)), bool(payload.get("manual")))
        except (TronApiError, OSError) as exc:
            error = f"{type(exc).__name__}: {str(exc)[:300]}"
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {str(exc)[:300]}"
            if not is_transient_db_error(exc):
                log.exception("JOB_FAILED", job_type=jtype, ref=ref, attempt=attempts)
        finally:
            self.running[jtype] -= 1
        if error:
            self.failed += 1
            log.warning("JOB_RETRY", job_type=jtype, ref=ref, attempt=attempts, error=error[:150])
            if jtype == "HISTORY":
                await self._mark_history_error(ref, error)
        else:
            self.completed += 1
        try:
            async with self.sf() as s, s.begin():
                await repo.finish_job(s, jid, self.clock.now(), error=error, retry_in=min(600, 15 * 2 ** min(attempts, 6)), max_attempts=12)
        except Exception as exc:  # noqa: BLE001 - job stays RUNNING -> reset at next start
            log.warning("JOB_FINISH_FAILED", job_type=jtype, ref=ref, error=type(exc).__name__)
        self.wakeup.set()

    async def _mark_history_error(self, wallet: str, error: str) -> None:
        try:
            async with self.sf() as s, s.begin():
                w = await repo.get_wallet(s, wallet)
                if w:
                    w.history_error = error[:500]
        except Exception:  # noqa: BLE001
            pass

    async def investigate(self, event_id: int) -> None:
        await self.investigator.investigate(event_id)
        async with self.sf() as s:
            ev = await s.get(PoisoningEvent, event_id)
            others = [e.id for e in await repo.events_for_suspicious(s, ev.suspicious_recipient) if e.id != event_id] if ev else []
        for oid in others:
            await self.investigator.refresh_other_victims(oid)

    async def trace(self, event_id: int, run: int, manual: bool) -> None:
        async with self.sf() as s:
            ev = await s.get(PoisoningEvent, event_id)
        if ev is None:
            return
        prev = await self.tracer.previous_signature(event_id, run - 1) if run > 1 else set()
        result = await self.tracer.trace(event_id, run)
        facts, summary = self.tracer.forwarding_facts(ev, result)
        if facts:
            await self.investigator.apply_findings(event_id, facts, dust_tri=None, forwarding_summary=summary, source="trace")
        changed = {h.transfer.transfer_key for h in result.hops} != prev
        now = self.clock.now()
        async with self.sf() as s, s.begin():
            if manual or run == 1 or changed:
                if not ev.is_historical or manual:
                    await self.notifier.event_alert(s, event_id, "TRACE", suffix=f"run{run}{':manual' if manual else ''}")
            # periodic re-trace while the trail may still be moving
            if not manual and self.s.trace_retrace_minutes > 0 and run < MAX_AUTO_TRACE_RUNS and not ev.is_historical:
                await repo.enqueue_job(
                    s, "TRACE", f"{event_id}:{run + 1}", now, {"event_id": event_id, "run": run + 1},
                    run_at=now + timedelta(minutes=self.s.trace_retrace_minutes),
                )  # fmt: skip
        self.notifier.wake()

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.claim_and_start()
            except Exception as exc:  # noqa: BLE001
                if not is_transient_db_error(exc):
                    log.exception("JOB_WORKER_ERROR", error=type(exc).__name__)
            self.wakeup.clear()
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

    async def drain(self, timeout: float = 30.0) -> None:
        """Run until no job is due or running (tests / simulation)."""
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            started = await self.claim_and_start()
            if self.tasks:
                await asyncio.wait(set(self.tasks), timeout=max(0.01, end - loop.time()))
                continue
            if not started:
                return
        raise TimeoutError("jobs did not finish in time")
