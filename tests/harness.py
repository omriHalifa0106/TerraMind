"""In-process harness: the real MCP servers in memory + the Mock Alerts API via ASGI transport.

The alerts server reads the database, so tests switch scenarios with `await set_scenario(...)`,
which also runs the alerts pipeline into the test database.

No ports, no subprocesses: used by the integration tests.
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


def mock_client() -> httpx.AsyncClient:
    """HTTP client that talks to the Mock Alerts API in memory (ASGI), no port needed."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_api.app), base_url="http://mock-alerts", timeout=5
    )


async def set_scenario(name: str) -> None:
    """Switch the mock scenario and run the alerts pipeline, as the API's scenario route does."""
    from ingestion import alerts_ingest
    from terramind.config import get_settings

    if name not in mock_api.SCENARIOS:
        raise ValueError(f"unknown scenario {name!r}; known: {sorted(mock_api.SCENARIOS)}")
    mock_api.app.state.current = name
    async with mock_client() as client:
        await alerts_ingest.run(get_settings().database_url, "test", client=client)


async def clear_stored_weather() -> None:
    """Forget stored forecasts, so a test's mocked Open-Meteo answer is the one that counts."""
    from mcp_servers.common.db import get_pool

    await (await get_pool()).execute("DELETE FROM weather_forecasts")


@asynccontextmanager
async def inprocess_gateway(timeout_s: float = 15) -> AsyncIterator[McpGateway]:
    gateway = McpGateway(
        {"gis": gis_mcp, "knowledge": knowledge_mcp, "weather": weather_mcp, "alerts": alerts_server.mcp},
        timeout_s=timeout_s,
    )
    # The in-process knowledge server has no startup warm-up (scripts.dev does one): load the
    # embedding model now, so the first search does not spend the tool timeout on loading it.
    from mcp_servers.knowledge_server.server import get_embedder

    await get_embedder().embed(["warm-up"])
    async with gateway:
        yield gateway
