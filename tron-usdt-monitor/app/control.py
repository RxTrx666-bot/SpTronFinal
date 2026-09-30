"""Server-side start/stop of monitoring.

Monitoring can only be started from the server (not from Telegram):

    docker compose exec tron-usdt-monitor python -m app.control start
    docker compose exec tron-usdt-monitor python -m app.control stop
    docker compose exec tron-usdt-monitor python -m app.control status

The request is written to the database; the running bot applies it within a few seconds.
"""

from __future__ import annotations

import asyncio
import os
import sys

from dotenv import find_dotenv, load_dotenv

from app.database import create_repository
from app.timeutil import now_ms
from app.tx_limit import STATE_CONTROL, STATE_CYCLE, STATE_PAUSED, STATE_START_ID, STATE_THRESHOLD

USAGE = "usage: python -m app.control start|stop|status"


async def run(action: str, database_url: str) -> str:
    repo = create_repository(database_url)
    await repo.init()
    try:
        paused = await repo.get_state(STATE_PAUSED) == "1"
        if action in ("start", "stop"):
            if action == "start" and not paused:
                return "Already running – monitoring is active."
            if action == "stop" and paused:
                return "Already paused."
            await repo.set_state(STATE_CONTROL, f"{action}:{now_ms()}")
            return ("Start requested – monitoring starts within a few seconds (count 0)."
                    if action == "start" else "Stop requested – monitoring pauses within a few seconds.")
        if action == "status":
            start_id = int(await repo.get_state(STATE_START_ID) or 0)
            count = await repo.count_live_transactions_after(start_id)
            threshold = await repo.get_state(STATE_THRESHOLD) or "?"
            cycle = await repo.get_state(STATE_CYCLE) or "1"
            state = "PAUSED" if paused else "RUNNING"
            return f"Monitoring: {state} | counter {count}/{threshold} | cycle #{cycle}"
        return USAGE
    finally:
        await repo.close()


def main(argv: list[str] | None = None) -> int:
    load_dotenv(find_dotenv(usecwd=True))
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] not in ("start", "stop", "status"):
        print(USAGE)
        return 2
    print(asyncio.run(run(args[0], os.environ.get("DATABASE_URL", "sqlite:///data/monitor.db"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
