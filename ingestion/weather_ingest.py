"""Weather pipeline: forecasts for every trailhead over the whole horizon, stored in weather_forecasts.

Trailheads are grouped into ~1 km grid cells (102 trails -> ~90 cells), and the cells are sent to
Open-Meteo in batches: one request returns 16 days for up to `weather_batch_size` points. A batch
that fails is reported in ingestion_runs and leaves the previously stored rows of its cells alone.

    uv run python -m ingestion.weather_ingest
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

import httpx

from ingestion.runs import IngestionFailed, Trigger, recorded_run
from mcp_servers.common.http import build_client
from mcp_servers.weather_server.open_meteo import build_batch_params, grid_key, parse_days, split_batch
from mcp_servers.weather_server.store import UPSERT, row_values
from terramind.config import get_settings
from terramind.db import connect

log = logging.getLogger("terramind.ingestion.weather")

RETRY_STATUS = {429, 500, 502, 503, 504}
ATTEMPTS = 3


async def get_json(client: httpx.AsyncClient, url: str, params: dict[str, str | int]) -> object:
    """GET with bounded retries on rate limits / server errors (connection retries are in the transport)."""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            resp = await client.get(url, params=params)
            if resp.status_code in RETRY_STATUS and attempt < ATTEMPTS:
                await asyncio.sleep(2**attempt)
                continue
            resp.raise_for_status()
            return resp.json()
        except httpx.TransportError:
            if attempt == ATTEMPTS:
                raise
            await asyncio.sleep(2**attempt)
    raise AssertionError("unreachable")


async def run(
    dsn: str | None = None, trigger: Trigger = "manual", client: httpx.AsyncClient | None = None
) -> int:
    """One pipeline run. Returns the number of forecast rows written."""
    s = get_settings()
    conn = await connect(dsn or s.database_url)
    own_client = client is None
    http = client or build_client()
    try:
        async with recorded_run(conn, "weather", trigger) as run_log:
            points = await conn.fetch("SELECT ST_Y(trailhead) AS lat, ST_X(trailhead) AS lon FROM trails")
            cells: list[tuple[Decimal, Decimal]] = sorted({grid_key(p["lat"], p["lon"]) for p in points})
            if not cells:
                raise IngestionFailed("no trails in the database: run the OSM loader first")
            size = max(1, s.weather_batch_size)
            for start in range(0, len(cells), size):
                batch = cells[start : start + size]
                try:
                    payload = await get_json(
                        http, s.open_meteo_url, build_batch_params(batch, s.forecast_days)
                    )
                    answers = split_batch(payload)
                    if len(answers) != len(batch):
                        raise ValueError(f"expected {len(batch)} locations, got {len(answers)}")
                    rows = [
                        row_values(cell, snap, run_log.run_id)
                        for cell, answer in zip(batch, answers, strict=True)
                        for snap in parse_days(answer)
                    ]
                except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                    run_log.failures.append(f"cells {start}-{start + len(batch) - 1}: {type(exc).__name__}")
                    log.warning("weather batch %d failed: %s", start, exc)
                    continue
                async with conn.transaction():
                    await conn.executemany(UPSERT, rows)
                run_log.rows += len(rows)
            if run_log.rows == 0:
                raise IngestionFailed(run_log.detail() or "no forecast rows")
            # Past days are of no use to a planner.
            await conn.execute("DELETE FROM weather_forecasts WHERE for_date < current_date - 1")
            log.info("weather: %d rows for %d cells (%s)", run_log.rows, len(cells), run_log.status)
            return run_log.rows
    finally:
        if own_client:
            await http.aclose()
        await conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    print("rows written:", asyncio.run(run()))
