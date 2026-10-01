"""Shared fixtures.

Tests run on SQLite by default.  Set TEST_DATABASE_URL to a PostgreSQL URL
(e.g. postgresql+asyncpg://postgres:postgres@localhost/tron_poison_test) to run
the same suite on PostgreSQL; each test then starts from an empty schema that
is created by the real SQL migrations.
"""

from __future__ import annotations

import os
import time

import pytest
from sqlalchemy import select, text

from app.config import Settings
from app.models import Alert, PoisoningEvent, PoisoningEvidence
from app.services.telegram_service import ConsoleTransport
from app.simulation import addresses as A
from app.simulation.chain import SimulatedChain

TEST_DB = os.environ.get("TEST_DATABASE_URL")
USDT = 10**6
DAY = 86_400_000
ADMIN = 1


def make_settings(db_url: str, **kw) -> Settings:
    base = dict(
        database_url=db_url,
        telegram_bot_token="",
        telegram_admin_chat_id=str(ADMIN),
        send_startup_message=False,
        heartbeat_file="/tmp/tron-poison-test.heartbeat",
        trace_retrace_minutes=0,
        labels_file="",
        alert_retry_base_seconds=0.01,
        alert_retry_max_seconds=0.02,
        tron_retry_base_seconds=0.01,
        tron_retry_max_seconds=0.02,
        tron_rate_limit_rps=0,
        pending_recovery_interval_seconds=0.05,
        block_poll_interval_seconds=0.01,
        network_wide=False,  # watched-wallet tests; network-wide mode is tested in test_network.py
    )
    base.update(kw)
    return Settings(_env_file=None, **base)


class Harness:
    def __init__(self, app, chain: SimulatedChain, transport: ConsoleTransport) -> None:
        self.app = app
        self.chain = chain
        self.t = transport

    @property
    def now_ms(self) -> int:
        return int(time.time() * 1000)

    def history(self, victim: str = A.VICTIM, legit: str = A.LEGIT, payments: int = 6, amount: int = 20_000 * USDT, *, start_days: int = 120, extra=()) -> None:
        """Give ``victim`` a payment history to ``legit`` (and optional extra (to, amount) payments)."""
        start = self.now_ms - start_days * DAY
        if self.chain.head_ts >= start:
            start = self.chain.head_ts + 3000
        self.chain.send(A.FUNDING_SOURCE, victim, 10_000_000 * USDT, ts_ms=start)
        for i in range(payments):
            self.chain.send(victim, legit, amount + i * USDT, ts_ms=start + (i + 1) * DAY)
        for j, (to, amt) in enumerate(extra):
            self.chain.send(victim, to, amt, ts_ms=start + (payments + j + 2) * DAY)

    async def add(self, address: str = A.VICTIM) -> None:
        res = await self.app.admin.add_wallet(address, added_by=ADMIN)
        assert res.ok, res.message
        await self.app.jobs.drain()
        await self.app.alerts.deliver_due()

    async def go_live(self) -> None:
        """Mine a fresh block near 'now' and establish the block cursor."""
        self.chain.mine(max(self.now_ms - 6000, self.chain.head_ts + 3000))
        await self.app.monitor.step()

    async def send_live(self, frm: str, to: str, amount: int, **kw) -> str:
        h = self.chain.send(frm, to, amount, ts_ms=max(self.now_ms, self.chain.head_ts + 3000), **kw)
        await self.app.monitor.step()
        return h

    async def settle(self) -> None:
        await self.app.jobs.drain(timeout=30)
        await self.app.alerts.deliver_due()

    async def events(self, **filters) -> list[PoisoningEvent]:
        async with self.app.sf() as s:
            q = select(PoisoningEvent).order_by(PoisoningEvent.id)
            for k, v in filters.items():
                q = q.where(getattr(PoisoningEvent, k) == v)
            return list((await s.execute(q)).scalars())

    async def evidence(self, event_id: int) -> list[PoisoningEvidence]:
        async with self.app.sf() as s:
            return list((await s.execute(select(PoisoningEvidence).where(PoisoningEvidence.event_id == event_id))).scalars())

    async def alerts(self, **filters) -> list[Alert]:
        async with self.app.sf() as s:
            q = select(Alert).order_by(Alert.id)
            for k, v in filters.items():
                q = q.where(getattr(Alert, k) == v)
            return list((await s.execute(q)).scalars())

    def sent(self, contains: str = "") -> list[dict]:
        return [m for m in self.t.messages if contains in m["text"]]


@pytest.fixture
def db_url(tmp_path):
    return TEST_DB or f"sqlite+aiosqlite:///{tmp_path}/test.db"


async def _reset_pg(url: str) -> None:
    from app.database import create_engine

    eng = create_engine(url)
    async with eng.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await eng.dispose()


@pytest.fixture
async def make_app(db_url):
    """Factory: make_app(chain=None, transport=None, fresh=True, **settings) -> Harness."""
    from app.main import Application

    created: list[Harness] = []
    reset = False

    async def factory(chain: SimulatedChain | None = None, transport: ConsoleTransport | None = None, fresh: bool = True, **kw) -> Harness:
        nonlocal reset
        if db_url.startswith("postgresql") and fresh and not reset:
            await _reset_pg(db_url)
            reset = True
        chain = chain or SimulatedChain()
        transport = transport or ConsoleTransport(echo=False)
        app = Application(make_settings(db_url, **kw), source=chain, transport=transport)
        await app.start()
        h = Harness(app, chain, transport)
        created.append(h)
        return h

    yield factory
    for h in created:
        try:
            await h.app.close()
        except Exception:
            pass
