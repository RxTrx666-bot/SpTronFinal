"""Wallet discovery engine + large transfer detector.

Every source (live stream, per-wallet scheduler, backfill) hands normalized
transfers to ``TransferProcessor.process``.  For each batch, in ONE database
transaction:

1. keep only transfers that matter: sent by an expanding wallet (Wallet A) or
   received by a monitored (discovered) wallet;
2. ``INSERT ... ON CONFLICT (tx_hash, event_index) DO NOTHING`` - a transfer we
   already stored is skipped entirely, which is what makes re-processing after
   a restart / retry / overlap harmless;
3. discovery: Wallet A -> new address  =>  insert into ``wallets``;
4. detection: monitored wallet receives >= ALERT_MIN_AMOUNT_USDT from anyone
   =>  insert into ``alerts`` (UNIQUE(tx_hash, event_index, alert_type)).

Because the transfer, the wallet and the alert commit atomically, a crash at
any point either leaves nothing (the batch is re-fetched and re-processed) or
everything (the batch is recognised as already done).  Telegram delivery
happens afterwards from the ``alerts`` table (see ``alerts.dispatcher``).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.exc import DBAPIError

from app.amounts import base_to_usdt, fmt_usdt
from app.config import Settings
from app.db import repository as repo
from app.domain import (
    ALERT_DISCOVERY,
    ALERT_LARGE_TRANSFER,
    ALERT_PENDING,
    ALERT_SUPPRESSED,
    KIND_BOTH,
    KIND_DISCOVERY,
    KIND_INCOMING,
    Transfer,
    ms_to_dt,
    utcnow,
)
from app.engine.registry import WalletInfo, WalletRegistry
from app.logging_setup import get_logger

log = get_logger(__name__)

SOURCE_STREAM = "stream"
SOURCE_RECONCILE = "reconcile"
SOURCE_BACKFILL = "backfill"


@dataclass
class ProcessResult:
    stored: int = 0
    duplicates: int = 0
    ignored: int = 0
    discovered: list[str] = field(default_factory=list)
    alerts: int = 0


@dataclass
class _BatchContext:
    source: str
    live: bool
    detected_at: datetime
    res: ProcessResult = field(default_factory=ProcessResult)
    overlay: dict[str, WalletInfo] = field(default_factory=dict)  # discovered in this uncommitted batch
    log_lines: list[tuple[str, dict]] = field(default_factory=list)

    def lookup(self, reg: WalletRegistry, address: str) -> WalletInfo | None:
        return self.overlay.get(address) or reg.get(address)


class TransferProcessor:
    def __init__(
        self,
        settings: Settings,
        session_factory,
        registry: WalletRegistry,
        *,
        monitor_started_ms: int,
        on_alert: Callable[[], None] | None = None,
        on_discovered: Callable[[list[str]], None] | None = None,
    ) -> None:
        self.s = settings
        self.sf = session_factory
        self.reg = registry
        self.monitor_started_ms = monitor_started_ms
        self.paused = False
        self.on_alert = on_alert
        self.on_discovered = on_discovered
        self.total_stored = 0
        self.total_seen = 0

    # ------------------------------------------------------------------ rules
    def alert_eligible(self, t: Transfer, *, live: bool) -> bool:
        """History before monitoring started never alerts unless ALERT_ON_HISTORICAL."""
        if self.s.alert_on_historical:
            return True
        return live and t.timestamp_ms >= self.monitor_started_ms

    def is_large(self, t: Transfer) -> bool:
        return t.amount_base_units >= self.s.alert_min_base_units  # >=, never >

    def is_relevant(self, t: Transfer) -> bool:
        return self.reg.is_expander(self.reg.get(t.from_address)) or self.reg.is_monitored(self.reg.get(t.to_address))

    # ------------------------------------------------------------------ main
    async def process(self, transfers: list[Transfer], *, source: str, live: bool = True) -> ProcessResult:
        self.total_seen += len(transfers)
        unique: dict[tuple[str, int], Transfer] = {}
        for t in transfers:
            unique.setdefault(t.key, t)
        batch = sorted(unique.values(), key=Transfer.sort_key)
        # Cheap pre-filter (no DB round-trip for the ~all irrelevant stream pages).
        # Wallets discovered inside the batch are handled by the overlay below.
        if not any(self.is_relevant(t) for t in batch):
            return ProcessResult(ignored=len(batch))
        for attempt in range(1, 4):
            try:
                return await self._process(batch, source=source, live=live)
            except DBAPIError as exc:  # concurrent writers can deadlock on the same keys: retry
                if "deadlock" not in str(exc).lower() or attempt == 3:
                    raise
                log.warning("Deadlock while storing batch; retrying", attempt=attempt)
                await asyncio.sleep(0.1 * attempt)
        raise AssertionError("unreachable")

    async def _process(self, batch: list[Transfer], *, source: str, live: bool) -> ProcessResult:
        ctx = _BatchContext(source=source, live=live, detected_at=utcnow())
        t0 = time.perf_counter()
        async with self.sf() as s, s.begin():
            # Pass 1 handles transfers sent by expanding wallets (discoveries) so
            # that pass 2 already knows every wallet discovered in this batch:
            # events in the same block share a timestamp and their order within
            # a block is not reliable.
            deferred = []
            for t in batch:
                if self.reg.is_expander(ctx.lookup(self.reg, t.from_address)):
                    await self._handle(s, t, ctx)
                else:
                    deferred.append(t)
            for t in deferred:
                await self._handle(s, t, ctx)
        db_ms = (time.perf_counter() - t0) * 1000
        res = ctx.res
        # Commit succeeded: publish to memory and wake the dependants.
        for info in ctx.overlay.values():
            self.reg.add(info)
        self.total_stored += res.stored
        for msg, fields in ctx.log_lines:
            (log.warning if msg.startswith("LARGE") else log.info)(msg, **fields, db_ms=f"{db_ms:.0f}")
        if res.stored:
            log.debug("Transfers stored", source=source, stored=res.stored, duplicates=res.duplicates, db_ms=f"{db_ms:.0f}")
        if res.discovered and self.on_discovered:
            self.on_discovered(res.discovered)
        if res.alerts and self.on_alert:
            self.on_alert()
        return res

    async def _handle(self, s, t: Transfer, ctx: "_BatchContext") -> None:
        res = ctx.res
        sender, recipient = ctx.lookup(self.reg, t.from_address), ctx.lookup(self.reg, t.to_address)
        from_expander = self.reg.is_expander(sender)
        to_monitored = self.reg.is_monitored(recipient)
        if not from_expander and not to_monitored:
            res.ignored += 1
            return
        kind = KIND_BOTH if from_expander and to_monitored else KIND_DISCOVERY if from_expander else KIND_INCOMING
        if not await repo.insert_transfer(s, t, kind=kind, source=ctx.source):
            res.duplicates += 1  # already processed earlier (restart / overlap / retry)
            return
        res.stored += 1

        # ---------------------------------------------- discovery
        if from_expander and recipient is None and t.to_address not in (t.from_address, self.reg.root):
            hop = sender.hop + 1
            if await repo.insert_discovered_wallet(s, address=t.to_address, root=self.reg.root, parent=t.from_address, hop=hop, t=t):
                recipient = WalletInfo(t.to_address, hop, t.timestamp_ms, t.tx_hash)
                ctx.overlay[t.to_address] = recipient
                res.discovered.append(t.to_address)
                # The scheduler checks this wallet from its discovery (or from
                # monitoring start for wallets found by backfill).
                await repo.init_checkpoint(s, t.to_address, ms_to_dt(max(t.timestamp_ms, self.monitor_started_ms)))
                ctx.log_lines.append(
                    (
                        "Wallet discovered",
                        dict(address=t.to_address, hop=hop, parent=t.from_address, amount=fmt_usdt(t.amount_base_units), tx=t.tx_hash, source=ctx.source),
                    )
                )
                if self.s.alert_on_discovery and self.alert_eligible(t, live=ctx.live):
                    if await repo.insert_alert(s, **self._alert_values(t, ALERT_DISCOVERY, recipient, ctx.detected_at)):
                        res.alerts += 1

        # ---------------------------------------------- detection
        if (
            self.reg.is_monitored(recipient)
            and self.is_large(t)
            and (t.from_address != t.to_address or self.s.alert_on_self_transfer)
            and t.timestamp_ms >= recipient.discovered_at_ms
            and self.alert_eligible(t, live=ctx.live)
        ):
            alert_id = await repo.insert_alert(s, **self._alert_values(t, ALERT_LARGE_TRANSFER, recipient, ctx.detected_at))
            if alert_id:
                res.alerts += 1
                ctx.log_lines.append(
                    (
                        "LARGE TRANSFER DETECTED",
                        dict(
                            alert_id=alert_id,
                            wallet=t.to_address,
                            sender=t.from_address,
                            amount=fmt_usdt(t.amount_base_units),
                            tx=t.tx_hash,
                            confirmed=t.confirmed,
                            chain_to_detect_ms=int(ctx.detected_at.timestamp() * 1000 - t.timestamp_ms),
                            paused=self.paused,
                        ),
                    )
                )

    def _alert_values(self, t: Transfer, alert_type: str, wallet: WalletInfo, detected_at) -> dict:
        return dict(
            tx_hash=t.tx_hash,
            event_index=t.event_index,
            alert_type=alert_type,
            discovered_wallet=t.to_address,
            sender=t.from_address,
            root_wallet=self.reg.root,
            amount_base_units=t.amount_base_units,
            amount_usdt=base_to_usdt(t.amount_base_units),
            block_number=t.block_number,
            transfer_timestamp=t.timestamp,
            confirmed=t.confirmed,
            discovery_tx=wallet.discovery_tx,
            discovered_at=ms_to_dt(wallet.discovered_at_ms),
            status=ALERT_SUPPRESSED if self.paused else ALERT_PENDING,
            attempts=0,
            detected_at=detected_at,
        )
