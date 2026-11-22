from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import date, time, timedelta
from typing import Any

import httpx
import pytest
import respx
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel

from evals.harness import inprocess_gateway, set_scenario
from mcp_servers.weather_server import server as weather_server
from terramind.llm.heuristic import HeuristicLLM
from terramind.llm.router import Task
from terramind.mcp_client.client import start_call_log
from terramind.runtime import AppRuntime, open_runtime
from terramind.schemas import TripRequest

pytestmark = pytest.mark.db


@pytest.fixture
async def rt(loaded_db: str) -> AsyncIterator[AppRuntime]:
    weather_server._cache.clear()
    async with inprocess_gateway() as gw, open_runtime(gateway=gw, persistent=False) as runtime:
        yield runtime
    set_scenario("normal")


async def turn(rt: AppRuntime, text: str, thread: str, user: str = "u1") -> dict[str, Any]:
    cfg: RunnableConfig = {"configurable": {"thread_id": thread}, "recursion_limit": 40}
    path: list[str] = []
    async for chunk in rt.graph.astream(
        {"messages": [HumanMessage(text)], "user_id": user}, cfg, context=rt.context, stream_mode="updates"
    ):
        path.extend(chunk)
    values = dict((await rt.graph.aget_state(cfg)).values)
    values["_path"] = path
    return values


async def test_closed_trail_is_replaced_and_explained(rt: AppRuntime) -> None:
    set_scenario("kziv_closed")
    calls = start_call_log()
    v = await turn(rt, "אני רוצה לטייל בנחל כזיב מחר, מסלול מים", "g1")
    final = v["final"]
    assert final.plan is not None and final.plan.trail_id != "osm-relation-9000001"
    # The veto goes straight back to the planner: other candidates were already found.
    assert v["_path"].count("safety_critic") == 2 and v["_path"].count("researcher") == 1
    assert any(
        t.rejected_trail_id == "osm-relation-9000001" and t.factor == "CLOSED" for t in final.tradeoffs
    )
    assert len(final.tradeoffs) == len(
        {(t.rejected_trail_id, t.factor) for t in final.tradeoffs}
    )  # no duplicates
    assert "gis.find_trails_by_name" in {f"{c['server']}.{c['tool']}" for c in calls}  # region inferred


async def test_everything_closed_ends_in_safe_fallback_without_a_plan(rt: AppRuntime) -> None:
    set_scenario("everything_closed")
    v = await turn(rt, "Hike in the Upper Galilee tomorrow", "g2")
    assert v["final"].plan is None
    assert "safe_fallback" in v["_path"] and v["attempts"] == 3
    assert v["_path"].count("safety_critic") == 3


async def test_smalltalk_and_missing_region_ask_back_without_tools(rt: AppRuntime) -> None:
    calls = start_call_log()
    v = await turn(rt, "שלום!", "g3")
    assert v["_path"][-1] == "ask_clarification" and calls == []
    v = await turn(rt, "I want a nice hike tomorrow", "g4")
    assert v["_path"][-1] == "ask_clarification"


async def test_follow_up_question_uses_the_stored_plan(rt: AppRuntime) -> None:
    await turn(rt, "Hike near Meron tomorrow", "g5")
    calls = start_call_log()
    v = await turn(rt, "Why this trail?", "g5")
    assert v["intent"] == "question" and "researcher" not in v["_path"] and calls == []


async def test_preferences_persist_across_threads(rt: AppRuntime) -> None:
    await turn(rt, "Easy water hike in the Upper Galilee tomorrow with 2 kids age 6", "g6", user="fam")
    v = await turn(rt, "Hike near Meron tomorrow", "g7", user="fam")
    assert v["request"].group.min_child_age == 6
    assert any("saved preferences" in a for a in v["assumptions"])


class LateStartLLM(HeuristicLLM):
    """Same as the heuristic model, but the user 'asked' to start at 14:30 on a short winter day."""

    async def structured[T: BaseModel](
        self,
        task: Task,
        schema: type[T],
        messages: Sequence[BaseMessage],
        config: RunnableConfig | None = None,
    ) -> T:
        result = await super().structured(task, schema, messages, config)
        if isinstance(result, TripRequest):
            return result.model_copy(update={"start_time": time(14, 30)})  # type: ignore[return-value]
        return result


async def test_daylight_veto_moves_the_start_earlier(rt: AppRuntime) -> None:
    rt.context.llm = LateStartLLM()
    d = date.today() + timedelta(days=1)
    payload = {
        "daily": {
            "temperature_2m_max": [16],
            "apparent_temperature_max": [15],
            "precipitation_probability_max": [0],
            "wind_gusts_10m_max": [15],
            "sunrise": [f"{d}T06:20"],
            "sunset": [f"{d}T16:40"],
        },
        "hourly": {"time": [], "precipitation_probability": []},
    }
    with respx.mock(assert_all_called=False) as m:
        m.get(url__startswith="https://api.open-meteo.com").mock(
            return_value=httpx.Response(200, json=payload)
        )
        v = await turn(rt, "Water hike in the Upper Galilee tomorrow", "g8")
    history = v["verdict_history"]
    assert history[0].replan_scope == "timing" and not history[0].approved
    assert history[-1].approved
    plan = v["final"].plan
    assert plan is not None and plan.start_time < time(14, 30) and plan.end_time <= time(15, 40)
    assert v["_path"].count("planner") == len(
        history
    )  # timing vetoes loop to the planner, not the researcher


class DownLLM:
    """Every call fails, like a rejected API key (HTTP 401)."""

    async def structured(self, *args: Any, **kwargs: Any) -> Any:
        from terramind.llm.client import LLMError

        raise LLMError("OpenRouter rejected the API key (HTTP 401).")

    async def agent_structured(self, *args: Any, **kwargs: Any) -> Any:
        return await self.structured()

    async def stream_text(self, *args: Any, **kwargs: Any) -> AsyncIterator[str]:
        await self.structured()
        yield ""  # pragma: no cover


async def test_llm_outage_degrades_to_offline_fallbacks(rt: AppRuntime) -> None:
    """Reproduces the 401 case: the trail is still found, vetoed, replaced and explained."""
    rt.context.llm = DownLLM()  # type: ignore[assignment]
    set_scenario("kziv_closed")
    v = await turn(rt, "אני רוצה לטייל בנחל כזיב מחר, מסלול מים", "g9")
    assert v["request"].region == "Upper Galilee"  # keyword extraction + named-trail lookup
    final = v["final"]
    assert final.plan is not None and final.plan.trail_id != "osm-relation-9000001"
    assert final.summary_md  # template answer instead of the streamed one
    assert {e.node for e in v["errors"]} >= {"supervisor", "intake", "planner", "explainer"}


async def test_enrich_matches_closures_on_every_trail_name(rt: AppRuntime) -> None:
    """OSM may keep the route number in `name` and the Hebrew name in `name_he`."""
    from datetime import date
    from types import SimpleNamespace

    from terramind.graph.nodes.planning import enrich
    from terramind.schemas import RoutePlan, Stop, TrailCandidate

    set_scenario("kziv_closed")
    trail = TrailCandidate(
        trail_id="osm-relation-424242",
        name="מסלול 2224",
        name_he="ליד נחל כזיב",
        region="r",
        length_km=3.2,
        trailhead=(33.05, 35.2),
    )
    stops = [
        Stop(order=0, name="a", kind="trailhead", location=(33.05, 35.2)),
        Stop(order=1, name="b", kind="end", location=(33.05, 35.2)),
    ]
    plan = RoutePlan(
        plan_id="p",
        trail_id=trail.trail_id,
        title="t",
        stops=stops,
        walk_km=3.2,
        walk_minutes=60,
        start_time=time(8),
        end_time=time(9),
    )
    state = {
        "draft_plan": plan,
        "candidates": [trail],
        "messages": [],
        "user_id": "u",
        "request": TripRequest(region="Upper Galilee", trip_date=date.today(), semantic_query="q"),
    }
    runtime = SimpleNamespace(context=rt.context, stream_writer=lambda _: None)
    out = await enrich(state, runtime)  # type: ignore[arg-type]
    assert out["trail_status"].is_open is False
    assert any(a.kind == "maintenance" for a in out["alerts"])


class RecordingLLM(HeuristicLLM):
    def __init__(self) -> None:
        self.explainer_data: str = ""

    def stream_text(
        self, task: Task, messages: Sequence[BaseMessage], config: RunnableConfig | None = None
    ) -> AsyncIterator[str]:
        self.explainer_data = str(messages[-1].content)
        return super().stream_text(task, messages, config)


async def test_explainer_gets_rationale_and_alternatives(rt: AppRuntime) -> None:
    rec = RecordingLLM()
    rt.context.llm = rec
    await turn(rt, "Water hike in the Upper Galilee tomorrow", "g10")
    import json

    data = json.loads(rec.explainer_data.split("DATA: ", 1)[1].split("\n\nUSER MESSAGE:")[0])
    assert data["chosen_trail"]["length_km"] > 0 and data["chosen_because"]
    assert data["alternatives"] and {"length_km", "tags", "difficulty"} <= set(data["alternatives"][0])
    assert data["request"]["trail_types"] == ["water"]


class NoRegionLLM(HeuristicLLM):
    """Like an LLM that leaves obvious fields empty ("near Meron" -> region None, no types)."""

    async def structured[T: BaseModel](
        self,
        task: Task,
        schema: type[T],
        messages: Sequence[BaseMessage],
        config: RunnableConfig | None = None,
    ) -> T:
        result = await super().structured(task, schema, messages, config)
        if isinstance(result, TripRequest):
            return result.model_copy(update={"region": None, "trail_types": [], "missing_fields": ["region"]})  # type: ignore[return-value]
        return result


async def test_empty_llm_fields_are_filled_from_keywords(rt: AppRuntime) -> None:
    rt.context.llm = NoRegionLLM()
    v = await turn(rt, "Easy water hike near Meron tomorrow", "g11")
    assert v["_path"][-1] != "ask_clarification"
    assert v["request"].region and "water" in v["request"].trail_types


async def test_named_trail_is_checked_first(rt: AppRuntime) -> None:
    set_scenario("kziv_closed")
    v = await turn(rt, "Hike Nahal Kziv tomorrow", "g12")
    assert v["named_trail_ids"] == ["osm-relation-9000001"]
    first = v["verdict_history"][0]
    assert not first.approved and first.rejected_trail_ids == ["osm-relation-9000001"]
    assert v["final"].plan is not None and v["final"].plan.trail_id != "osm-relation-9000001"


class PlannerSpyLLM(HeuristicLLM):
    """Records the planner prompt and fails the test if the old tool-calling loop is used."""

    def __init__(self) -> None:
        self.planner_calls: list[str] = []

    async def structured[T: BaseModel](
        self,
        task: Task,
        schema: type[T],
        messages: Sequence[BaseMessage],
        config: RunnableConfig | None = None,
    ) -> T:
        if task == "planner":
            self.planner_calls.append(str(messages[-1].content))
        return await super().structured(task, schema, messages, config)

    async def agent_structured(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the planner must not run a tool-calling loop")


async def test_planner_decides_in_one_call_over_prefetched_pois(rt: AppRuntime) -> None:
    spy = PlannerSpyLLM()
    rt.context.llm = spy
    calls = start_call_log()
    v = await turn(rt, "Water hike in the Upper Galilee tomorrow", "g13")
    assert v["final"].plan is not None
    assert len(spy.planner_calls) == 1  # one attempt, one LLM call
    assert "POIS BY TRAIL:" in spy.planner_calls[0]
    pois = [c for c in calls if c["tool"] == "find_pois_near_trail"]
    assert 1 <= len(pois) <= 3  # fetched once per top candidate, not again after the choice


async def test_trail_veto_replans_without_searching_again(rt: AppRuntime) -> None:
    set_scenario("kziv_closed")
    calls = start_call_log()
    v = await turn(rt, "Hike Nahal Kziv tomorrow", "g14")
    assert not v["verdict_history"][0].approved and v["verdict_history"][-1].approved
    assert v["_path"].count("researcher") == 1 and v["_path"].count("grader") == 1
    assert sum(c["tool"] == "search_trails" for c in calls) == 1
