from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client

from mcp_servers.gis_server.server import mcp as gis
from mcp_servers.knowledge_server.server import mcp as knowledge
from mcp_servers.weather_server import server as weather_server
from terramind.mcp_client.client import McpGateway
from tests.harness import clear_stored_weather, inprocess_gateway, set_scenario

pytestmark = pytest.mark.db


async def call(server: Any, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    async with Client(server) as c:
        res = await c.call_tool(tool, args)
        return dict(res.structured_content or {})


async def test_spatial_query_finds_water_trails_near_a_point(loaded_db: str) -> None:
    r = await call(
        gis, "search_trails", {"tags": ["water"], "near_lat": 33.04, "near_lon": 35.2, "max_distance_km": 20}
    )
    assert r["status"] == "ok"
    assert r["trails"][0]["name"] == "שביל נחל כזיב" and "water" in r["trails"][0]["tags"]
    assert all(t["distance_km"] <= 20 for t in r["trails"])


async def test_exclude_ids_and_unknown_region(loaded_db: str) -> None:
    r = await call(gis, "search_trails", {"region": "Upper Galilee", "exclude_ids": ["osm-relation-9000001"]})
    assert "osm-relation-9000001" not in {t["trail_id"] for t in r["trails"]}
    assert (await call(gis, "search_trails", {"region": "Mars"}))["status"] == "empty"


async def test_find_trails_by_name_in_hebrew_sentence(loaded_db: str) -> None:
    r = await call(gis, "find_trails_by_name", {"text": "אני רוצה לטייל בנחל כזיב מחר"})
    assert [t["trail_id"] for t in r["trails"]] == ["osm-relation-9000001"]


async def test_pois_and_geojson(loaded_db: str) -> None:
    pois = await call(gis, "find_pois_near_trail", {"trail_id": "osm-relation-9000001"})
    assert {p["category"] for p in pois["pois"]} >= {"spring", "ruins"}
    # Measured along the trail line from the trailhead (parking end), in walking order.
    assert all(p["along_trail"] for p in pois["pois"])
    distances = [p["distance_m"] for p in pois["pois"]]
    assert distances == sorted(distances) and distances[0] < 50  # the parking lot is at the trailhead
    geo = await call(gis, "get_trail_geojson", {"trail_id": "osm-relation-9000002"})
    assert geo["feature"]["geometry"]["type"] == "MultiLineString"


async def test_hebrew_semantic_query_returns_relevant_chunks(loaded_db: str) -> None:
    r = await call(knowledge, "hybrid_search", {"query": "מסלול מים מוצל למשפחות", "k": 5})
    assert r["status"] == "ok" and len(r["chunks"]) == 5
    assert any("מים" in c["text"] for c in r["chunks"][:3])
    linked = await call(
        knowledge, "hybrid_search", {"query": "waterfall", "trail_ids": ["osm-relation-9000004"]}
    )
    assert {c["trail_id"] for c in linked["chunks"]} <= {"osm-relation-9000004", None}


async def test_weather_store_horizon_and_timeout(loaded_db: str) -> None:
    """A live answer is stored, and a nearby trailhead (same ~1 km cell) is then served from the table."""
    await clear_stored_weather()
    d = date.today() + timedelta(days=2)
    payload = {
        "daily": {
            "temperature_2m_max": [24],
            "precipitation_probability_max": [10],
            "sunset": [f"{d}T16:45"],
        },
        "hourly": {"time": [], "precipitation_probability": []},
    }
    with respx.mock(assert_all_called=False) as m:
        route = m.get(url__startswith="https://api.open-meteo.com").mock(
            return_value=httpx.Response(200, json=payload)
        )
        a = await call(
            weather_server.mcp,
            "get_forecast",
            {"trail_id": "a", "lat": 33.05, "lon": 35.18, "for_date": d.isoformat()},
        )
        b = await call(
            weather_server.mcp,
            "get_forecast",
            {"trail_id": "b", "lat": 33.051, "lon": 35.181, "for_date": d.isoformat()},
        )
        assert (a["status"], b["status"], route.call_count) == ("ok", "cached", 1)
        far = (date.today() + timedelta(days=40)).isoformat()
        assert (
            await call(
                weather_server.mcp, "get_forecast", {"trail_id": "c", "lat": 33, "lon": 35, "for_date": far}
            )
        )["status"] == "out_of_range"
        route.mock(side_effect=httpx.ConnectTimeout("boom"))
        t = await call(
            weather_server.mcp,
            "get_forecast",
            {"trail_id": "d", "lat": 32.1, "lon": 34.9, "for_date": d.isoformat()},
        )
        assert t["status"] == "unavailable"


async def test_alerts_through_gateway_and_missing_server(loaded_db: str) -> None:
    async with inprocess_gateway() as gw:
        await set_scenario("kziv_closed")
        st = await gw.call("alerts", "get_trail_status", {"trail_id": "osm-relation-9000001"})
        assert st["trail"]["is_open"] is False
        await set_scenario("normal")
        st = await gw.call("alerts", "get_trail_status", {"trail_id": "osm-relation-9000001"})
        assert st["trail"]["is_open"] is True
    down_gw = McpGateway({"weather": "http://127.0.0.1:9/mcp"}, timeout_s=2)  # nothing listens on port 9
    async with down_gw:
        down = await down_gw.call("weather", "get_forecast", {"trail_id": "x"})
        assert down["status"] == "unavailable" and down_gw.connected == []


async def test_drive_leg_uses_osrm_and_falls_back_and_geocode() -> None:
    """estimate_leg(drive): real roads from OSRM; if OSRM is down, the straight-line estimate."""
    from mcp_servers.gis_server import routing

    osrm = {
        "code": "Ok",
        "routes": [
            {
                "distance": 51382.7,
                "duration": 2922.9,
                "geometry": {"coordinates": [[34.99, 32.79], [35.25, 33.03]]},
            }
        ],
    }
    nominatim = [{"lat": "32.794", "lon": "34.989", "display_name": "חיפה, נפת חיפה, מחוז חיפה, ישראל"}]
    leg_args = {"from_lat": 32.79, "from_lon": 34.99, "to_lat": 33.03, "to_lon": 35.25, "mode": "drive"}
    routing.set_client(None)  # a fresh client inside the respx mock
    with respx.mock(assert_all_called=False) as m:
        route = m.get(url__regex=r".*/route/v1/driving/.*").mock(return_value=httpx.Response(200, json=osrm))
        m.get(url__startswith="https://nominatim.openstreetmap.org").mock(
            return_value=httpx.Response(200, json=nominatim)
        )
        leg = await call(gis, "estimate_leg", leg_args)
        assert (leg["method"], leg["minutes"], leg["distance_km"]) == ("routing_engine", 49, 51.4)
        assert leg["route"][0] == [32.79, 34.99]  # [lat, lon] for the map

        route.mock(side_effect=httpx.ConnectError("osrm down"))
        fallback = await call(gis, "estimate_leg", leg_args)
        assert fallback["method"] == "geodesic_factor" and fallback["minutes"] > 0 and fallback["route"] == []

        places = await call(gis, "geocode", {"query": "חיפה"})
        assert places["status"] == "ok" and places["places"][0]["label"] == "חיפה, נפת חיפה, מחוז חיפה"
    routing.set_client(None)
