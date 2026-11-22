"""weather-server: forecasts and daylight hours for trailheads over MCP.

Reads the forecasts the data pipeline stored (ingestion/weather_ingest.py). When a cell/date is
missing or older than WEATHER_MAX_AGE_H, it fetches that one day from Open-Meteo live and stores
it, so the next request is served from the table. If the database is down it still answers live.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from decimal import Decimal

import asyncpg
import httpx
from fastmcp import FastMCP

from mcp_servers.common.db import get_pool
from mcp_servers.common.http import build_client
from mcp_servers.weather_server import store
from mcp_servers.weather_server.open_meteo import build_params, grid_key, parse_snapshot
from terramind.config import get_settings
from terramind.schemas import Frozen, WeatherSnapshot

log = logging.getLogger("terramind.mcp.weather")
mcp = FastMCP("terramind-weather", instructions="Weather forecast and daylight for trailheads (Open-Meteo).")

FORECAST_HORIZON_DAYS = 16
DB_ERRORS = (asyncpg.PostgresError, OSError, TimeoutError)
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


async def _stored(key: tuple[Decimal, Decimal], for_date: date) -> asyncpg.Record | None:
    try:
        return await store.fetch_fresh(
            await get_pool(), key, for_date, get_settings().weather_max_age_h * 3600
        )
    except DB_ERRORS as exc:
        log.warning("weather table unavailable, answering live: %s", type(exc).__name__)
        return None


async def _save(key: tuple[Decimal, Decimal], snapshot: WeatherSnapshot) -> None:
    try:
        await (await get_pool()).execute(store.UPSERT, *store.row_values(key, snapshot, None))
    except DB_ERRORS as exc:
        log.warning("could not store live forecast: %s", type(exc).__name__)


async def _forecast(trail_id: str, lat: float, lon: float, for_date: date) -> WeatherSnapshot:
    if for_date > date.today() + timedelta(days=FORECAST_HORIZON_DAYS):
        return WeatherSnapshot.unavailable(trail_id, for_date, status="out_of_range")
    key = grid_key(lat, lon)
    if (row := await _stored(key, for_date)) is not None:
        return store.from_row(trail_id, row)
    try:
        resp = await _get_client().get(get_settings().open_meteo_url, params=build_params(lat, lon, for_date))
        resp.raise_for_status()
        snapshot = parse_snapshot(trail_id, for_date, resp.json())
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        # The safety rules treat this as DATA_UNAVAILABLE (a warning), never as "safe".
        log.warning("Open-Meteo failed for %s: %s", trail_id, type(exc).__name__)
        return WeatherSnapshot.unavailable(trail_id, for_date, status="unavailable")
    await _save(key, snapshot)
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
    mcp.run(transport="http", host=get_settings().mcp_bind_host, port=8103)
