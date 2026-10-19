"""Container health check: the poll loop must have written the heartbeat file recently."""

from __future__ import annotations

import os
import sys
import time


def main() -> int:
    path = os.environ.get("HEARTBEAT_FILE", "")
    max_age = int(os.environ.get("HEARTBEAT_MAX_AGE_SECONDS", "120"))
    if not path:
        return 0
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        print("no heartbeat yet")
        return 1
    if age > max_age:
        print(f"heartbeat is {int(age)}s old")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
