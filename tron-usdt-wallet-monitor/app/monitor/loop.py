"""A loop that never dies: API / DB failures back off exponentially and retry."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from app.db.session import is_transient_db_error
from app.logging_setup import get_logger
from app.tron.client import TronApiError

log = get_logger(__name__)


async def sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=max(0.0, seconds))
    except asyncio.TimeoutError:
        pass


async def run_forever(name: str, fn: Callable[[], Awaitable[Any]], interval: float, stop: asyncio.Event) -> None:
    """Call ``fn`` every ``interval`` seconds; immediately again if it returns True."""
    delay = 1.0
    while not stop.is_set():
        try:
            more = bool(await fn())
            delay = 1.0
        except asyncio.CancelledError:
            raise
        except TronApiError as exc:
            log.error("TRON API unavailable; backing off", loop=name, error=str(exc)[:200], retry_in=f"{delay:.0f}s")
            await sleep_or_stop(stop, delay)
            delay = min(delay * 2, 60)
            continue
        except Exception as exc:  # noqa: BLE001
            if is_transient_db_error(exc):
                log.error("Database unavailable; backing off", loop=name, error=type(exc).__name__, retry_in=f"{delay:.0f}s")
            else:
                log.exception("Loop error; backing off", loop=name)
            await sleep_or_stop(stop, delay)
            delay = min(delay * 2, 60)
            continue
        if not more:
            await sleep_or_stop(stop, interval)
