"""Monitored-wallet administration (used by Telegram commands and simulation)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import func, select

from app import repository as repo
from app.domain import HistoryStatus, WalletStatus
from app.models import WatchedWallet
from app.services.poisoning_detector import WalletRegistry
from app.utils.address import InvalidAddress, base58_to_hex, normalize_address
from app.utils.clock import Clock
from app.utils.logging import get_logger

log = get_logger(__name__)

STATE_ALERTS_PAUSED = "alerts_paused"
_LABEL_OK = re.compile(r"[^\w .,:@#()/&+-]", re.UNICODE)


@dataclass
class AdminResult:
    ok: bool
    message: str
    address: str | None = None


def clean_label(label: str | None) -> str | None:
    if not label:
        return None
    label = _LABEL_OK.sub("", label).strip()[:60]
    return label or None


class AdminService:
    def __init__(self, settings, session_factory, clock: Clock, registry: WalletRegistry, jobs_wakeup, audit=None) -> None:
        self.s = settings
        self.sf = session_factory
        self.clock = clock
        self.registry = registry
        self.jobs_wakeup = jobs_wakeup
        self.audit = audit
        self.alerts_paused = False

    async def load(self) -> None:
        async with self.sf() as s:
            self.alerts_paused = (await repo.get_state(s, STATE_ALERTS_PAUSED)) == "1"

    def _audit(self, event: str, message: str, wallet: str | None = None, **data) -> None:
        log.info(event, wallet=wallet, **data)
        if self.audit:
            self.audit(event, message, wallet=wallet, data=data)

    @staticmethod
    def parse_address(text: str) -> str:
        return normalize_address(text.strip())

    async def add_wallet(self, address_text: str, *, label: str | None = None, added_by: int | None = None) -> AdminResult:
        try:
            address = self.parse_address(address_text)
        except InvalidAddress:
            return AdminResult(False, "❌ Invalid TRON address. Expected a Base58 address starting with T (34 characters) or a 41… hex address.")
        if address in self.s.tokens_by_contract:
            return AdminResult(False, "❌ That is a token contract, not a wallet.")
        now = self.clock.now()
        async with self.sf() as s, s.begin():
            w = await repo.get_wallet(s, address)
            if w is not None and w.status != WalletStatus.REMOVED.value:
                return AdminResult(False, f"ℹ️ Already monitored ({w.status.lower()}).", address)
            if w is None:
                w = WatchedWallet(
                    address=address,
                    address_hex=base58_to_hex(address),
                    label=clean_label(label),
                    status=WalletStatus.ACTIVE.value,
                    added_by=added_by,
                    added_at=now,
                    updated_at=now,
                    history_status=HistoryStatus.PENDING.value,
                    history_transfers_scanned=0,
                    history_truncated=False,
                )
                s.add(w)
                restarted = False
            else:
                w.status = WalletStatus.ACTIVE.value
                w.label = clean_label(label) or w.label
                w.added_by = added_by
                w.updated_at = now
                restarted = True
                if w.history_status != HistoryStatus.COMPLETE.value:
                    w.history_status = HistoryStatus.PENDING.value
            await s.flush()
            if w.history_status != HistoryStatus.COMPLETE.value:
                await repo.enqueue_job(s, "HISTORY", address, now, reset=True)
        self.registry.set(address, WalletStatus.ACTIVE.value)
        self.jobs_wakeup.set()
        self._audit("WALLET_ADDED", f"wallet added{' (re-activated)' if restarted else ''}", wallet=address, by=added_by)
        return AdminResult(
            True,
            f"✅ Now monitoring <code>{address}</code>\nLive monitoring is active immediately. "
            "Historical scan started — you will get a summary when it finishes.",
            address,
        )

    async def remove_wallet(self, address_text: str) -> AdminResult:
        try:
            address = self.parse_address(address_text)
        except InvalidAddress:
            return AdminResult(False, "❌ Invalid TRON address.")
        async with self.sf() as s, s.begin():
            w = await repo.get_wallet(s, address)
            if w is None or w.status == WalletStatus.REMOVED.value:
                return AdminResult(False, "ℹ️ That wallet is not monitored.", address)
            w.status = WalletStatus.REMOVED.value
            w.updated_at = self.clock.now()
        self.registry.set(address, None)
        self._audit("WALLET_REMOVED", "wallet removed", wallet=address)
        return AdminResult(True, f"🗑 Stopped monitoring <code>{address}</code> (history kept).", address)

    async def set_wallet_status(self, address_text: str, status: WalletStatus) -> AdminResult:
        try:
            address = self.parse_address(address_text)
        except InvalidAddress:
            return AdminResult(False, "❌ Invalid TRON address.")
        async with self.sf() as s, s.begin():
            w = await repo.get_wallet(s, address)
            if w is None or w.status == WalletStatus.REMOVED.value:
                return AdminResult(False, "ℹ️ That wallet is not monitored.", address)
            w.status = status.value
            w.updated_at = self.clock.now()
        self.registry.set(address, status.value)
        self._audit("WALLET_" + status.value, f"wallet {status.value.lower()}", wallet=address)
        verb = "⏸ Alerts paused" if status == WalletStatus.PAUSED else "▶️ Alerts resumed"
        return AdminResult(True, f"{verb} for <code>{address}</code>.", address)

    async def set_global_pause(self, paused: bool) -> AdminResult:
        async with self.sf() as s, s.begin():
            await repo.set_state(s, STATE_ALERTS_PAUSED, "1" if paused else "0", self.clock.now())
        self.alerts_paused = paused
        self._audit("ALERTS_PAUSED" if paused else "ALERTS_RESUMED", "global alert delivery " + ("paused" if paused else "resumed"))
        if paused:
            return AdminResult(True, "⏸ Alert delivery paused. Monitoring continues; alerts are queued and delivered on /resume.")
        return AdminResult(True, "▶️ Alert delivery resumed. Queued alerts are being delivered.")

    async def list_wallets(self) -> list[WatchedWallet]:
        async with self.sf() as s:
            q = select(WatchedWallet).where(WatchedWallet.status != WalletStatus.REMOVED.value).order_by(WatchedWallet.added_at)
            return list((await s.execute(q)).scalars())

    async def wallet_counts(self) -> dict[str, int]:
        async with self.sf() as s:
            rows = await s.execute(select(WatchedWallet.status, func.count()).group_by(WatchedWallet.status))
            return {k: v for k, v in rows.all()}
