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
            resp = await c.post("/chat/stream", json={"message": "מסלול מים בגליל העליון מחר"})
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
            geo = await c.get(f"/trails/{final['plan']['trail_id']}/geojson")
            assert geo.status_code == 200 and geo.json()["type"] == "Feature"
            assert (await c.get("/trails/nope/geojson")).status_code == 404
            health = (await c.get("/health")).json()
            assert sorted(health["mcp_servers"]) == ["alerts", "gis", "knowledge", "weather"]
            fresh = (await c.get("/pipeline")).json()  # set_scenario above ran the alerts pipeline
            assert fresh["alerts"]["last_status"] == "ok" and fresh["alerts"]["last_good_at"]
    await set_scenario("normal")
