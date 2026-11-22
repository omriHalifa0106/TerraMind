"""Start the whole stack locally (mock alerts API, 4 MCP servers, API) in one terminal.

    uv run python -m scripts.dev            # Ctrl+C stops everything

Works on Windows, macOS and Linux. The database runs separately: `docker compose up -d db`.
Checks the database first, then waits for every MCP server to listen before starting the API.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass

PY = sys.executable


@dataclass(frozen=True)
class Service:
    name: str
    cmd: list[str]
    health_url: str | None = None  # HTTP health route, if the service has one
    port: int | None = None  # otherwise wait until this TCP port accepts connections
    wait_s: float = 60
    note: str | None = None


SERVICES = [
    Service(
        "mock-alerts",
        [PY, "-m", "uvicorn", "services.mock_alerts_api.main:app", "--port", "8200"],
        health_url="http://127.0.0.1:8200/health",
    ),
    Service("mcp-gis", [PY, "-m", "mcp_servers.gis_server.server"], port=8101),
    # Loads bge-m3 before it starts listening; the first run downloads ~2.3 GB.
    Service(
        "mcp-knowledge",
        [PY, "-m", "mcp_servers.knowledge_server.server"],
        port=8102,
        wait_s=1800,
        note="loading the embedding model (the first run downloads ~2.3 GB, this can take a while)",
    ),
    Service("mcp-weather", [PY, "-m", "mcp_servers.weather_server.server"], port=8103),
    Service("mcp-alerts", [PY, "-m", "mcp_servers.alerts_server.server"], port=8104),
]
API = Service("api", [PY, "-m", "scripts.run_api"], health_url="http://127.0.0.1:8000/health", wait_s=120)

Procs = list[tuple[str, subprocess.Popen[bytes]]]


def _crashed(procs: Procs) -> str | None:
    return next((name for name, p in procs if p.poll() is not None), None)


def wait_ready(svc: Service, procs: Procs) -> None:
    deadline = time.monotonic() + svc.wait_s
    while time.monotonic() < deadline:
        if (dead := _crashed(procs)) is not None:
            raise SystemExit(f"ERROR: {dead} exited during startup (see its log above)")
        if svc.health_url:
            try:
                with urllib.request.urlopen(svc.health_url, timeout=2):
                    return
            except OSError:
                pass
        elif svc.port:
            with socket.socket() as sock:
                sock.settimeout(1)
                if sock.connect_ex(("127.0.0.1", svc.port)) == 0:
                    return
        time.sleep(1)
    raise SystemExit(f"ERROR: {svc.name} was not ready after {svc.wait_s:.0f}s")


def preflight() -> None:
    """Check the database before starting anything, with a message that says what to do."""
    from terramind.config import get_settings
    from terramind.db import DatabaseSetupError, connect

    async def check() -> None:
        await (await connect(get_settings().database_url, connect_timeout=5)).close()

    try:
        asyncio.run(check())
    except DatabaseSetupError as exc:
        raise SystemExit(
            f"ERROR: {exc}\n\nIs Docker Desktop running? Start it, then: docker compose up -d db"
        ) from None


def main() -> None:
    preflight()
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    # On Windows each child gets its own process group, so CTRL_BREAK stops it without killing us.
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    procs: Procs = []
    try:
        for svc in SERVICES:
            procs.append((svc.name, subprocess.Popen(svc.cmd, env=env, creationflags=flags)))
        for svc in SERVICES:
            if svc.note:
                print(f"  waiting for {svc.name}: {svc.note}", flush=True)
            wait_ready(svc, procs)
        procs.append((API.name, subprocess.Popen(API.cmd, env=env, creationflags=flags)))
        wait_ready(API, procs)
        print("\n  TerraMind is up:  http://127.0.0.1:8000\n", flush=True)
        while _crashed(procs) is None:
            time.sleep(1)
        print(f"process exited: {_crashed(procs)}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        for _, p in reversed(procs):
            if p.poll() is None:
                p.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT)
        for _, p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    main()
