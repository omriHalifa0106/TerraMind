from __future__ import annotations

import json

import httpx
import pytest

from terramind.api.main import app
from terramind.runtime import open_runtime
from terramind.schemas import TrailType, TripRequest
from tests.fakes import FakeLLM
from tests.harness import inprocess_gateway, set_scenario

pytestmark = pytest.mark.db


def parse_sse(body: str) -> list[tuple[str, dict]]:  # type: ignore[type-arg]
    events = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        etype, data = "message", ""
        for line in block.split("\n"):
            if line.startswith("event:"):
                etype = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        if data:
            events.append((etype, json.loads(data)))
    return events


async def test_chat_stream_emits_the_agents_events(loaded_db: str) -> None:
    await set_scenario("kziv_closed")
    req = TripRequest(region="Upper Galilee", trail_types=[TrailType.WATER], semantic_query="water hike")
    async with inprocess_gateway() as gw, open_runtime(gateway=gw) as rt:
        rt.context.llm = FakeLLM(req)
        app.state.rt = rt  # skip the lifespan: no ports
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t", timeout=60) as c:
            haifa = {"lat": 32.79, "lon": 34.99, "label": "חיפה"}
            resp = await c.post(
                "/chat/stream", json={"message": "מסלול מים בגליל העליון מחר", "origin": haifa}
            )
            events = parse_sse(resp.text)
            kinds = [e for e, _ in events]
            assert kinds[0] == "start" and kinds[-1] == "done"
            assert {"request", "tool_calls", "candidates", "assessment", "plan", "token", "final"} <= set(
                kinds
            )
            vetoes = [
                d["assessment"] for e, d in events if e == "assessment" and not d["assessment"]["approved"]
            ]
            assert vetoes and vetoes[0]["issues"][0]["code"] == "CLOSED"
            final = next(d["final"] for e, d in events if e == "final")
            # The starting point from the UI reached agent 4: a drive leg and a time to leave home
            # (OSRM when it runs locally, otherwise the straight-line fallback).
            plan = final["plan"]
            assert plan["origin_label"] == "חיפה" and plan["drive_minutes"] > 0 and plan["depart_at"]
            geo = await c.get(f"/trails/{final['plan']['trail_id']}/geojson")
            assert geo.status_code == 200 and geo.json()["type"] == "Feature"
            assert (await c.get("/trails/nope/geojson")).status_code == 404
            health = (await c.get("/health")).json()
            assert sorted(health["mcp_servers"]) == ["alerts", "gis", "knowledge", "weather"]
            fresh = (await c.get("/pipeline")).json()  # set_scenario above ran the alerts pipeline
            assert fresh["alerts"]["last_status"] == "ok" and fresh["alerts"]["last_good_at"]
    await set_scenario("normal")


async def test_a_follow_up_edits_the_same_trip(loaded_db: str) -> None:
    """Same chat, second message only changes the day: agents 2-3 are skipped, the same trail is kept,
    and agent 1 saw the previous turn. A new chat id starts from scratch."""
    import uuid

    await set_scenario("normal")
    first = TripRequest(region="Upper Galilee", trail_types=[TrailType.WATER], semantic_query="water hike")
    thread = uuid.uuid4().hex
    async with inprocess_gateway() as gw, open_runtime(gateway=gw) as rt:
        app.state.rt = rt
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t", timeout=60) as c:

            async def turn(llm: FakeLLM, text: str, thread_id: str) -> list[tuple[str, dict]]:  # type: ignore[type-arg]
                rt.context.llm = llm
                resp = await c.post("/chat/stream", json={"message": text, "thread_id": thread_id})
                return parse_sse(resp.text)

            # Named so the fake model picks Kziv, the fixture trail with places around it.
            with_cart = first.model_copy(update={"want_places": ["coffee_cart"], "day_hours": 8})
            one = await turn(FakeLLM(with_cart), "יום של 8 שעות בנחל כזיב עם עגלת קפה", thread)
            plan1 = next(d["plan"] for e, d in one if e == "plan")
            assert [s["category"] for s in plan1["stops"] if s.get("place_id")] == ["coffee_cart"]

            add = with_cart.model_copy(update={"want_places": ["coffee_cart", "winery"]})
            edit = FakeLLM(add, change="edit")
            two = await turn(edit, "תוסיף יקב", thread)
            ran = [d["node"] for e, d in two if e == "node"]
            assert ran == ["request_analyst", "route_planner", "presenter"]  # no new search or checks
            assert next(d for e, d in two if e == "scope")["scope"] == "replan"
            plan2 = next(d["plan"] for e, d in two if e == "plan")
            assert plan2["trail_id"] == plan1["trail_id"]
            kinds = [s["category"] for s in plan2["stops"] if s.get("place_id")]
            assert "coffee_cart" in kinds  # kept: the edit only asked to add
            assert "winery" in kinds or any("יקב" in d for d in plan2["dropped_places"])
            assert "PREVIOUS REQUEST" in edit.prompts["conversation"]
            assert next(d for e, d in two if e == "start")["follow_up"] is True

            other = await turn(FakeLLM(first), "מסלול מים בגליל העליון", uuid.uuid4().hex)
            assert next(d for e, d in other if e == "start")["follow_up"] is False
            assert "catalog_search" in [d["node"] for e, d in other if e == "node"]
