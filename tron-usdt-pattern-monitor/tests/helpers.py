from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select

from app.clock import ManualClock
from app.collector.address import address_from_seed
from app.database import repository as repo
from app.database.models import Alert, Transaction
from app.domain import datetime_to_ms
from app.main import Application
from app.simulation.source import SimulatedEventSource, make_event
from app.telegram.alerts import RecordingSink


def addr(name: str) -> str:
    return address_from_seed(f"test-wallet-{name}")


@dataclass
class Harness:
    app: Application
    src: SimulatedEventSource
    rec: RecordingSink
    clock: ManualClock

    @property
    def contract(self) -> str:
        return self.app.settings.usdt_contract_address

    def ev(self, s: str, r: str, amount, when: datetime, *, unconfirmed=False, txid=None, contract=None) -> dict:
        return self.src.add(
            make_event(
                sender=addr(s),
                recipient=addr(r),
                amount_usdt=amount,
                ts_ms=datetime_to_ms(when),
                contract=contract or self.contract,
                txid=txid,
                unconfirmed=unconfirmed,
            )
        )

    async def poll(self, *, unconfirmed: bool = False) -> list[str]:
        """Run the collector once (as the live loop would), then learning + delivery."""
        before = len(self.rec.messages)
        if unconfirmed:
            await self.app.collector.poll_unconfirmed_once()
        else:
            while await self.app.collector.poll_confirmed_once():
                pass
        await self.app.analysis.drain()
        await self.app.dispatcher.flush()
        return self.rec.messages[before:]

    async def history(self, pairs: dict[tuple[str, str], list[tuple[float, float, float]]], start: datetime) -> None:
        """Load historic (test, large, minutes_between) sequences and run a backfill."""
        for (s, r), seqs in pairs.items():
            t = start
            for test, large, minutes in seqs:
                self.ev(s, r, test, t)
                self.ev(s, r, large, t + timedelta(minutes=minutes))
                t += timedelta(days=1)

    async def backfill(self, start: datetime) -> list[str]:
        """Run the real backfill (collector windows) over [start, now)."""
        before = len(self.rec.messages)
        async with self.app.session_factory() as s, s.begin():
            now = self.clock.now()
            await repo.set_state(s, "backfill_next_ms", str(datetime_to_ms(start)), now)
            await repo.set_state(s, "backfill_end_ms", str(datetime_to_ms(now)), now)
            await repo.set_state(s, "backfill_complete", "0", now)
            await repo.set_state(s, "backfill_analysis_complete", "0", now)
        await self.app.collector.run_backfill(self.app.stop_event, self.app.on_backfill_complete)
        await self.app.dispatcher.flush()
        return self.rec.messages[before:]

    async def entry(self, s: str, r: str):
        async with self.app.session_factory() as ss:
            return await repo.get_watchlist(ss, addr(s), addr(r))

    async def alerts(self, alert_type: str | None = None) -> list[Alert]:
        async with self.app.session_factory() as ss:
            q = select(Alert).order_by(Alert.id)
            if alert_type:
                q = q.where(Alert.alert_type == alert_type)
            return list((await ss.execute(q)).scalars())

    async def tx_count(self) -> int:
        async with self.app.session_factory() as ss:
            return int((await ss.execute(select(func.count()).select_from(Transaction))).scalar_one())

    async def tx(self, txid: str) -> Transaction:
        async with self.app.session_factory() as ss:
            return (await ss.execute(select(Transaction).where(Transaction.transaction_hash == txid))).scalar_one()


def kinds(msgs: list[str]) -> list[str]:
    out = []
    for m in msgs:
        if "WATCHLIST TEST TRANSFER DETECTED" in m:
            out.append("TEST")
        elif "LARGE FOLLOW-UP DETECTED" in m:
            out.append("FOLLOWUP")
        elif "AUTOMATIC WATCHLIST ACTIVATED" in m:
            out.append("ACTIVATED")
        elif "NEW USDT TEST" in m:
            out.append("NEW_PATTERN")
        else:
            out.append("OTHER")
    return out
