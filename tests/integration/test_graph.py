"""The five agents end to end: real MCP servers and PostGIS (synthetic fixture), a fake LLM,
and a fixed weather forecast so the outcome does not depend on today's weather."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import date, time, timedelta
from typing import Any

import httpx
import pytest
import respx
from langchain_core.messages import HumanMessage

from terramind.graph.nodes.enrichment import _conditions
from terramind.graph.state import turn_input
from terramind.mcp_client.client import start_call_log
from terramind.runtime import AppRuntime, open_runtime
from terramind.schemas import TrailCandidate, TrailType, TripRequest
from tests.fakes import DownLLM, FakeLLM
from tests.harness import clear_stored_weather, inprocess_gateway, set_scenario

pytestmark = pytest.mark.db
KZIV = "osm-relation-9000001"
TOMORROW = date.today() + timedelta(days=1)
GALILEE_WATER = TripRequest(
    region="Upper Galilee", trip_date=TOMORROW, trail_types=[TrailType.WATER], semantic_query="water hike"
)


@pytest.fixture
def calm_weather() -> Iterator[None]:
    """Mild, dry, sunset 16:40 (a December day)."""
    payload = {
        "daily": {
            "temperature_2m_max": [16],
            "apparent_temperature_max": [15],
            "precipitation_probability_max": [0],
            "wind_gusts_10m_max": [15],
            "sunrise": [f"{TOMORROW}T06:20"],
            "sunset": [f"{TOMORROW}T16:40"],
        },
        "hourly": {"time": [], "precipitation_probability": []},
    }
    with respx.mock(assert_all_called=False) as m:
        m.get(url__startswith="https://api.open-meteo.com").mock(
            return_value=httpx.Response(200, json=payload)
        )
        yield


@pytest.fixture
async def rt(loaded_db: str, calm_weather: None) -> AsyncIterator[AppRuntime]:
    await clear_stored_weather()
    async with inprocess_gateway() as gw, open_runtime(gateway=gw) as runtime:
        yield runtime
    await set_scenario("normal")


async def run(rt: AppRuntime, llm: FakeLLM, text: str) -> dict[str, Any]:
    rt.context.llm = llm
    path: list[str] = []
    final: dict[str, Any] = {}
    async for mode, chunk in rt.graph.astream(
        turn_input([HumanMessage(text)]),
        {"recursion_limit": 25},
        context=rt.context,
        stream_mode=["values", "updates"],
    ):
        if isinstance(chunk, dict):
            if mode == "values":
                final = dict(chunk)
            else:
                path.extend(chunk)
    final["_path"] = path
    return final


async def test_closed_trail_is_vetoed_before_planning_and_explained(rt: AppRuntime) -> None:
    await set_scenario("kziv_closed")
    llm = FakeLLM(GALILEE_WATER)
    v = await run(rt, llm, "אני רוצה לטייל בנחל כזיב מחר, מסלול מים")
    assert v["_path"] == ["request_analyst", "catalog_search", "enrichment", "route_planner", "presenter"]
    assert v["candidates"][0].trail_id == KZIV  # the named trail is checked first...
    kziv = next(a for a in v["assessments"] if a.trail_id == KZIV)
    assert not kziv.approved and {i.code for i in kziv.issues} == {"CLOSED"}  # ...and vetoed by agent 3
    plan = v["final"].plan
    assert plan is not None and plan.trail_id != KZIV  # agent 4 never saw it
    assert KZIV not in llm.prompts["route_planner"]
    assert [(t.rejected_trail_id, t.factor) for t in v["final"].tradeoffs] == [(KZIV, "CLOSED")]
    assert '"tradeoffs": [{' in llm.prompts["presenter"]  # agent 5 gets the veto with its evidence


async def test_everything_closed_searches_again_then_gives_no_plan(rt: AppRuntime) -> None:
    await set_scenario("everything_closed")
    calls = start_call_log()
    v = await run(rt, FakeLLM(GALILEE_WATER), "Water hike in the Upper Galilee tomorrow")
    assert v["_path"].count("catalog_search") == 2 and "route_planner" not in v["_path"]
    assert v["final"].plan is None and v["final"].safety_notes
    searches = [c for c in calls if c["tool"] == "search_trails"]
    assert searches[1]["args"]["exclude_ids"]  # round 2 excludes what round 1 vetoed
    assert "tags" not in searches[1]["args"] or searches[1]["args"]["tags"] == []  # and is wider


async def test_missing_region_asks_back_and_stops(rt: AppRuntime) -> None:
    calls = start_call_log()
    req = TripRequest(semantic_query="nice hike", missing_fields=["region"])
    v = await run(rt, FakeLLM(req), "I want a nice hike tomorrow")
    assert v["_path"] == ["request_analyst"] and "Which area" in v["clarification"]
    assert {c["tool"] for c in calls} <= {"find_trails_by_name"}


async def test_named_trail_gives_the_region(rt: AppRuntime) -> None:
    req = TripRequest(semantic_query="Nahal Kziv")  # the model found no region
    v = await run(rt, FakeLLM(req), "Hike Nahal Kziv tomorrow")
    assert v["request"].region == "Upper Galilee" and v["named_trail_ids"] == [KZIV]
    assert v["final"].plan is not None and v["final"].plan.trail_id == KZIV


async def test_late_start_is_moved_earlier_by_the_rules(rt: AppRuntime) -> None:
    late = GALILEE_WATER.model_copy(update={"start_time": time(14, 30)})
    v = await run(rt, FakeLLM(late), "Water hike tomorrow at 14:30")
    plan = v["final"].plan
    assert plan is not None and plan.start_time == time(6, 35)  # sunrise 06:20 + 15 minutes
    assert plan.end_time <= time(15, 40)  # sunset 16:40 minus the 60-minute buffer
    assert any("start moved to 06:35" in n for n in v["final"].safety_notes)


async def test_agent_2_runs_the_model_tool_calls_and_enforces_exclusions(rt: AppRuntime) -> None:
    await set_scenario("everything_closed")
    scripted = [
        {"name": "search_trails", "args": {"region": "Upper Galilee", "tags": ["water"], "exclude_ids": []}},
        {"name": "hybrid_search", "args": {"query": "מסלול מים"}},
    ]
    llm = FakeLLM(GALILEE_WATER, tool_calls=scripted)
    calls = start_call_log()
    await run(rt, llm, "Water hike in the Upper Galilee tomorrow")
    assert llm.tools_offered == ["hybrid_search", "search_trails"]
    searches = [c for c in calls if c["tool"] == "search_trails"]
    assert searches[0]["args"]["tags"] == ["water"]  # the model's arguments were used
    assert searches[1]["args"]["exclude_ids"]  # but vetoed trails are excluded even though it sent []
    assert next(c["args"]["query"] for c in calls if c["tool"] == "hybrid_search") == "מסלול מים"


async def test_llm_outage_ends_with_a_message_not_a_crash(rt: AppRuntime) -> None:
    v = await run(rt, DownLLM(GALILEE_WATER), "Water hike tomorrow")
    assert v["_path"] == ["request_analyst"] and v["clarification"]
    assert v["errors"][0].kind == "llm_error"


async def test_closures_match_every_trail_name(rt: AppRuntime) -> None:
    """OSM may keep the route number in `name` and the Hebrew name in `name_he`: the alerts
    pipeline must match a closure on either of them."""
    from mcp_servers.common.db import get_pool

    pool = await get_pool()
    original = await pool.fetchrow("SELECT name, name_he FROM trails WHERE trail_id = $1", KZIV)
    await pool.execute(
        "UPDATE trails SET name = 'מסלול 2224', name_he = 'ליד נחל כזיב' WHERE trail_id = $1", KZIV
    )
    try:
        await set_scenario("kziv_closed")
        trail = TrailCandidate(
            trail_id=KZIV,
            name="מסלול 2224",
            name_he="ליד נחל כזיב",
            region="r",
            length_km=3.2,
            trailhead=(33.05, 35.2),
        )
        _, status, alerts = await _conditions(rt.context.tools, trail, GALILEE_WATER)
    finally:
        await pool.execute(
            "UPDATE trails SET name = $2, name_he = $3 WHERE trail_id = $1",
            KZIV,
            original["name"],
            original["name_he"],
        )
    assert status is not None and status.is_open is False
    assert any(a.kind == "maintenance" for a in alerts)
