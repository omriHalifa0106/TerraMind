"""weather-server: Open-Meteo forecasts and daylight hours over MCP, with a TTL cache."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

import httpx
from cachetools import TTLCache
from fastmcp import FastMCP

from mcp_servers.common.http import build_client
from mcp_servers.weather_server.open_meteo import build_params, parse_snapshot
from terramind.config import get_settings
from terramind.schemas import Frozen, WeatherSnapshot

log = logging.getLogger("terramind.mcp.weather")
mcp = FastMCP("terramind-weather", instructions="Weather forecast and daylight for trailheads (Open-Meteo).")

FORECAST_HORIZON_DAYS = 16
_cache: TTLCache[tuple[float, float, date], WeatherSnapshot] = TTLCache(maxsize=512, ttl=1800)
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = build_client()
    return _client


class ForecastPoint(Frozen):
    trail_id: str
    lat: float
    lon: float


class ForecastBatch(Frozen):
    snapshots: list[WeatherSnapshot]


async def _forecast(trail_id: str, lat: float, lon: float, for_date: date) -> WeatherSnapshot:
    if for_date > date.today() + timedelta(days=FORECAST_HORIZON_DAYS):
        return WeatherSnapshot.unavailable(trail_id, for_date, status="out_of_range")
    key = (round(lat, 2), round(lon, 2), for_date)  # ~1 km grid
    if (hit := _cache.get(key)) is not None:
        return hit.model_copy(update={"trail_id": trail_id, "status": "cached"})
    try:
        resp = await _get_client().get(get_settings().open_meteo_url, params=build_params(lat, lon, for_date))
        resp.raise_for_status()
        snapshot = parse_snapshot(trail_id, for_date, resp.json())
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        # The critic treats this as DATA_UNAVAILABLE (a warning), never as "safe".
        log.warning("Open-Meteo failed for %s: %s", trail_id, type(exc).__name__)
        return WeatherSnapshot.unavailable(trail_id, for_date, status="unavailable")
    _cache[key] = snapshot
    return snapshot


@mcp.tool
async def get_forecast(trail_id: str, lat: float, lon: float, for_date: date) -> WeatherSnapshot:
    """Daily and hourly forecast for a trailhead on a date. Never raises: check `status`
    (ok / cached / out_of_range beyond 16 days / unavailable on API failure)."""
    return await _forecast(trail_id, lat, lon, for_date)


@mcp.tool
async def get_forecast_batch(points: list[ForecastPoint], for_date: date) -> ForecastBatch:
    """Forecasts for several trailheads at once (parallel requests, one tool call)."""
    snapshots = await asyncio.gather(*(_forecast(p.trail_id, p.lat, p.lon, for_date) for p in points[:20]))
    return ForecastBatch(snapshots=list(snapshots))


@mcp.tool
async def get_daylight(lat: float, lon: float, for_date: date) -> WeatherSnapshot:
    """Sunrise and sunset (local time) for a point and date; other fields may be filled too."""
    return await _forecast("daylight", lat, lon, for_date)


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8103)
