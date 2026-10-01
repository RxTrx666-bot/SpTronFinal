"""Health endpoint (``GET /health`` -> 200 ok / 503 degraded) and CLI probe.

``python -m app.health`` exits 0 when the endpoint reports ok; Docker's
HEALTHCHECK uses it.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.request

from sqlalchemy import text

from app.logging_setup import get_logger
from app.monitor.loop import sleep_or_stop
from app.runtime import Runtime

log = get_logger(__name__)


async def serve_health(rt: Runtime, stop: asyncio.Event) -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await asyncio.wait_for(reader.readline(), timeout=5)
            path = request.decode(errors="replace").split(" ")[1] if request.count(b" ") >= 2 else "/"
            if path.split("?")[0] in ("/health", "/healthz", "/"):
                ok, body = rt.health()
                status = "200 OK" if ok else "503 Service Unavailable"
                payload = json.dumps(body).encode()
            else:
                status, payload = "404 Not Found", b'{"error":"not found"}'
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
                + payload
            )
            await writer.drain()
        except Exception:  # noqa: BLE001
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, rt.settings.health_host, rt.settings.health_port)
    log.info("Health endpoint listening", url=f"http://{rt.settings.health_host}:{rt.settings.health_port}/health")
    async with server:
        await stop.wait()


async def watch_database(rt: Runtime, engine, stop: asyncio.Event) -> None:
    """Keeps ``rt.db_ok`` current for /health."""
    while not stop.is_set():
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            if not rt.db_ok:
                log.info("Database connection restored")
            rt.db_ok = True
        except Exception as exc:  # noqa: BLE001
            if rt.db_ok:
                log.error("Database connection lost", error=type(exc).__name__)
            rt.db_ok = False
        await sleep_or_stop(stop, 15)


def probe() -> int:
    port = os.environ.get("HEALTH_PORT", "8080")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as resp:  # noqa: S310
            print(resp.read().decode())
            return 0 if resp.status == 200 else 1
    except Exception as exc:  # noqa: BLE001
        print(f"unhealthy: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(probe())
