"""Structured logging.

Text format:  ``2026-09-23T13:15:32Z [INFO] Known watchlist test detected sender=T... amount=5``
JSON format:  one JSON object per line (LOG_FORMAT=json).

Use ``log = get_logger(__name__)`` then ``log.info("message", key=value, ...)``.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any


class _Formatter(logging.Formatter):
    def __init__(self, fmt: str) -> None:
        super().__init__()
        self.json = fmt == "json"

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        fields: dict[str, Any] = getattr(record, "fields", {}) or {}
        if self.json:
            payload = {"ts": ts, "level": record.levelname, "logger": record.name, "msg": record.getMessage()}
            payload.update({k: _jsonable(v) for k, v in fields.items()})
            if record.exc_info:
                payload["exc"] = self.formatException(record.exc_info)
            return json.dumps(payload, ensure_ascii=False)
        extra = " ".join(f"{k}={_text(v)}" for k, v in fields.items())
        line = f"{ts} [{record.levelname}] {record.getMessage()}" + (f" {extra}" if extra else "")
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def _jsonable(v: Any) -> Any:
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


def _text(v: Any) -> str:
    s = str(v)
    return f'"{s}"' if " " in s else s


class StructuredLogger:
    def __init__(self, name: str) -> None:
        self._log = logging.getLogger(name)

    def _emit(self, level: int, msg: str, exc_info: Any = None, **fields: Any) -> None:
        if self._log.isEnabledFor(level):
            self._log.log(level, msg, exc_info=exc_info, extra={"fields": fields})

    def debug(self, msg: str, **fields: Any) -> None:
        self._emit(logging.DEBUG, msg, **fields)

    def info(self, msg: str, **fields: Any) -> None:
        self._emit(logging.INFO, msg, **fields)

    def warning(self, msg: str, **fields: Any) -> None:
        self._emit(logging.WARNING, msg, **fields)

    def error(self, msg: str, exc_info: Any = None, **fields: Any) -> None:
        self._emit(logging.ERROR, msg, exc_info=exc_info, **fields)

    def exception(self, msg: str, **fields: Any) -> None:
        self._emit(logging.ERROR, msg, exc_info=True, **fields)

    def is_debug(self) -> bool:
        return self._log.isEnabledFor(logging.DEBUG)


def get_logger(name: str) -> StructuredLogger:
    return StructuredLogger(name)


def configure_logging(level: str = "INFO", fmt: str = "text") -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_Formatter(fmt))
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for noisy in ("httpx", "httpcore", "sqlalchemy.engine", "aiosqlite", "asyncio", "alembic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
