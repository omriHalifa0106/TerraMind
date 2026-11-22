"""alerts-server: read-only access to trail status, closures and alerts (Mock Alerts API)."""

from __future__ import annotations

import logging
from datetime import date

import httpx
from fastmcp import FastMCP
from pydantic import ValidationError

from mcp_servers.common.http import build_client
from terramind.config import get_settings
from terramind.schemas import TrailAlert, TrailStatus
from terramind.schemas.tool_results import AlertsResult, TrailStatusResult

log = logging.getLogger("terramind.mcp.alerts")
mcp = FastMCP("terramind-alerts", instructions="Trail closures, crowding and safety alerts. Read-only.")

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = build_client(get_settings().mock_alerts_url)
    return _client


@mcp.tool
async def get_trail_status(
    trail_id: str, trail_name: str | None = None, for_date: date | None = None
) -> TrailStatusResult:
    """Whether a trail is open on a date, why not, and how crowded it is expected to be."""
    params: dict[str, str] = {"trail_id": trail_id, "on": (for_date or date.today()).isoformat()}
    if trail_name:
        params["name"] = trail_name
    try:
        resp = await _get_client().get("/trails/status", params=params)
        resp.raise_for_status()
        status = TrailStatus.model_validate(resp.json())
    except (httpx.HTTPError, ValidationError, ValueError) as exc:
        log.warning("trail status unavailable: %s", type(exc).__name__)
        return TrailStatusResult(
            status="unavailable",
            detail=f"alerts service unreachable ({type(exc).__name__})",
            trail=TrailStatus(trail_id=trail_id, is_open=True, status="unavailable"),
        )
    return TrailStatusResult(status="ok", trail=status)


@mcp.tool
async def list_active_alerts(
    region: str | None = None,
    trail_id: str | None = None,
    trail_name: str | None = None,
    for_date: date | None = None,
) -> AlertsResult:
    """Active alerts (flood, fire, closure, military, maintenance, crowding) for a region and/or trail."""
    params: dict[str, str] = {"on": (for_date or date.today()).isoformat()}
    for key, value in (("region", region), ("trail_id", trail_id), ("name", trail_name)):
        if value:
            params[key] = value
    try:
        resp = await _get_client().get("/alerts", params=params)
        resp.raise_for_status()
        alerts = [TrailAlert.model_validate(a) for a in resp.json()]
    except (httpx.HTTPError, ValidationError, ValueError) as exc:
        return AlertsResult(status="unavailable", detail=f"alerts service unreachable ({type(exc).__name__})")
    return AlertsResult(status="ok" if alerts else "empty", alerts=alerts)


if __name__ == "__main__":
    mcp.run(transport="http", host=get_settings().mcp_bind_host, port=8104)
