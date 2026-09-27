"""Docker/systemd health probe: exits 0 if the monitor completed a poll recently."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from dotenv import find_dotenv, load_dotenv


def main() -> int:
    load_dotenv(find_dotenv(usecwd=True))
    path = Path(os.environ.get("HEARTBEAT_FILE", "data/heartbeat"))
    max_age = float(os.environ.get("HEALTHCHECK_MAX_AGE_SECONDS", "180"))
    try:
        last_ms = int(path.read_text().strip())
    except (OSError, ValueError):
        print("no heartbeat yet")
        return 1
    age = time.time() - last_ms / 1000
    if age > max_age:
        print(f"stale heartbeat: {age:.0f}s old")
        return 1
    print(f"ok: last poll {age:.1f}s ago")
    return 0


if __name__ == "__main__":
    sys.exit(main())
