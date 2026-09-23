"""Container health check: healthy while the maintenance loop keeps writing its heartbeat.

    python -m app.healthcheck      # exit 0 = healthy
"""

import sys
import time
from pathlib import Path

from app.config.settings import get_settings

MAX_AGE_SECONDS = 180


def main() -> int:
    try:
        ts = int(Path(get_settings().heartbeat_file).read_text().strip())
    except (OSError, ValueError):
        return 1
    return 0 if time.time() - ts < MAX_AGE_SECONDS else 1


if __name__ == "__main__":
    sys.exit(main())
