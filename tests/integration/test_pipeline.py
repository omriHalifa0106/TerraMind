"""The data pipeline: weather and alerts land in the database, the MCP servers serve them from there,
and failed or stale data is reported, never silently trusted."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client

from ingestion import weather_ingest
from ingestion.runs import IngestionFailed
from mcp_servers.alerts_server import server as alerts_server
from mcp_servers.common.db import get_pool
from mcp_servers.weather_server import server as weather_server
from tests.harness import clear_stored_weather, set_scenario

pytestmark = pytest.mark.db
KZIV = "osm-relation-9000001"


async def call(server: Any, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    async with Client(server) as c:
        return dict((await c.call_tool(tool, args)).structured_content or {})


def _batch_answer(request: httpx.Request) -> httpx.Response:
    """Open-Meteo stand-in: one entry per requested point, two days each."""
    n = len(request.url.params["latitude"].split(","))
    today = date.today()
    point = {
        "daily": {
            "time": [today.isoformat(), (today + timedelta(days=1)).isoformat()],
            "temperature_2m_max": [19, 23],
            "precipitation_probability_max": [0, 40],
            "sunset": [f"{today}T16:40", f"{today + timedelta(days=1)}T16:41"],
        },
        "hourly": {"time": [], "precipitation_probability": []},
    }
    return httpx.Response(200, json=[point] * n if n > 1 else point)


async def _last_run(source: str) -> Any:
    return await (await get_pool()).fetchrow(
        "SELECT status, rows_written, detail FROM ingestion_runs "
        "WHERE source = $1 ORDER BY run_id DESC LIMIT 1",
        source,
    )


async def test_weather_pipeline_stores_forecasts_served_by_the_mcp_server(loaded_db: str) -> None:
    await clear_stored_weather()
    with respx.mock(assert_all_called=False) as m:
        route = m.get(url__startswith="https://api.open-meteo.com").mock(side_effect=_batch_answer)
        rows = await weather_ingest.run(loaded_db, "test")
        assert rows > 0 and route.call_count == 1  # every trailhead in one batched request
        run = await _last_run("weather")
        assert (run["status"], run["rows_written"]) == ("ok", rows)

        trail = await (await get_pool()).fetchrow(
            "SELECT ST_Y(trailhead) AS lat, ST_X(trailhead) AS lon FROM trails WHERE trail_id = $1", KZIV
        )
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        w = await call(
            weather_server.mcp,
            "get_forecast",
            {"trail_id": KZIV, "lat": trail["lat"], "lon": trail["lon"], "for_date": tomorrow},
        )
        assert (w["status"], w["temp_max_c"], w["precip_prob_max_pct"]) == ("cached", 23, 40)
        assert route.call_count == 1  # answered from the table, not from Open-Meteo

        # A failed source is recorded and leaves the stored forecasts untouched.
        route.mock(return_value=httpx.Response(503))
        with pytest.raises(IngestionFailed):
            await weather_ingest.run(loaded_db, "test")
        assert (await _last_run("weather"))["status"] == "failed"
        count = await (await get_pool()).fetchval("SELECT count(*) FROM weather_forecasts")
        assert count == rows


async def test_alerts_pipeline_snapshot_and_staleness(loaded_db: str) -> None:
    await set_scenario("kziv_closed")
    run = await _last_run("alerts")
    assert run["status"] == "ok" and run["rows_written"] > 0
    st = await call(alerts_server.mcp, "get_trail_status", {"trail_id": KZIV})
    assert st["status"] == "ok" and st["trail"]["is_open"] is False

    # Switching back replaces the snapshot: the closure is gone.
    await set_scenario("normal")
    st = await call(alerts_server.mcp, "get_trail_status", {"trail_id": KZIV})
    assert st["trail"]["is_open"] is True

    # Old data is not trusted: the answer becomes "unavailable" (a warning for the safety rules).
    pool = await get_pool()
    await pool.execute(
        "UPDATE ingestion_runs SET finished_at = now() - interval '3 hours' WHERE source = 'alerts'"
    )
    try:
        st = await call(alerts_server.mcp, "get_trail_status", {"trail_id": KZIV})
        alerts = await call(alerts_server.mcp, "list_active_alerts", {"trail_id": KZIV})
        assert st["status"] == "unavailable" and "min old" in st["detail"]
        assert alerts["status"] == "unavailable"
    finally:
        await set_scenario("normal")  # a fresh run again for the tests that follow
