from __future__ import annotations

import httpx
import pytest

from outflow_alert.monitor import Monitor
from outflow_alert.storage import Storage
from outflow_alert.telegram import TelegramClient
from outflow_alert.tron_client import TronClient
from tests.fakes import FakeTelegram, FakeTronGrid, make_settings


@pytest.fixture
def grid() -> FakeTronGrid:
    return FakeTronGrid()


@pytest.fixture
def tg() -> FakeTelegram:
    return FakeTelegram()


@pytest.fixture
def build(grid, tg, tmp_path):
    """Factory for a Monitor wired to the fakes; reuse ``db_path`` to simulate a restart."""
    created = []

    def _build(db_path: str | None = None, **overrides):
        settings = make_settings(db_path=db_path or str(tmp_path / "state.db"), **overrides)
        tron = TronClient("https://api.trongrid.io", settings.tron_api_key, max_attempts=1,
                          transport=httpx.MockTransport(grid.handler))
        telegram = TelegramClient(settings.telegram_bot_token, transport=httpx.MockTransport(tg.handler))
        storage = Storage(settings.db_path)
        monitor = Monitor(settings, storage, tron, telegram)
        monitor.bind_state()
        created.append((tron, telegram, storage))
        return monitor

    yield _build
    for _, _, storage in created:
        storage.close()
