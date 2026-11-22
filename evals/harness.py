"""In-process harness: the real MCP servers in memory + the Mock Alerts API via ASGI transport.

No ports, no subprocesses: used by the eval runner, the CLI demo and the integration tests.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from mcp_servers.alerts_server import server as alerts_server
from mcp_servers.gis_server.server import mcp as gis_mcp
from mcp_servers.knowledge_server.server import mcp as knowledge_mcp
from mcp_servers.weather_server.server import mcp as weather_mcp
from services.mock_alerts_api import main as mock_api
from terramind.mcp_client.client import McpGateway


def set_scenario(name: str) -> None:
    if name not in mock_api.SCENARIOS:
        raise ValueError(f"unknown scenario {name!r}; known: {sorted(mock_api.SCENARIOS)}")
    mock_api.app.state.current = name


@asynccontextmanager
async def inprocess_gateway(timeout_s: float = 15) -> AsyncIterator[McpGateway]:
    # Route the alerts server's HTTP calls straight into the mock FastAPI app.
    alerts_server._client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_api.app), base_url="http://mock-alerts", timeout=5
    )
    gateway = McpGateway(
        {"gis": gis_mcp, "knowledge": knowledge_mcp, "weather": weather_mcp, "alerts": alerts_server.mcp},
        timeout_s=timeout_s,
    )
    # The in-process knowledge server has no startup warm-up (scripts.dev does one): load the
    # embedding model now, so the first search does not spend the tool timeout on loading it.
    from mcp_servers.knowledge_server.server import get_embedder

    await get_embedder().embed(["warm-up"])
    try:
        async with gateway:
            yield gateway
    finally:
        await alerts_server._client.aclose()
        alerts_server._client = None
