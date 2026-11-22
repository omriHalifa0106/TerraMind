from __future__ import annotations

import json

import httpx
import pytest

from evals.harness import inprocess_gateway, set_scenario
from terramind.api.main import app
from terramind.runtime import open_runtime

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


async def test_chat_stream_emits_typed_events_and_thread_state(loaded_db: str) -> None:
    set_scenario("kziv_closed")
    async with inprocess_gateway() as gw, open_runtime(gateway=gw, persistent=False) as rt:
        app.state.rt = rt  # skip the lifespan: no ports, in-memory checkpointer
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t", timeout=60) as c:
            resp = await c.post(
                "/chat/stream", json={"message": "מסלול מים בגליל העליון מחר", "thread_id": "api-1"}
            )
            events = parse_sse(resp.text)
            kinds = [e for e, _ in events]
            assert kinds[0] == "start" and kinds[-1] == "done"
            assert {"route", "candidates", "plan", "verdict", "token", "final"} <= set(kinds)
            vetoes = [d for e, d in events if e == "verdict" and not d["approved"]]
            assert vetoes and vetoes[0]["issues"][0]["code"] == "CLOSED"
            state = (await c.get("/threads/api-1")).json()
            assert state["final"]["plan"] is not None and len(state["messages"]) == 2
            geo = await c.get(f"/trails/{state['final']['plan']['trail_id']}/geojson")
            assert geo.status_code == 200 and geo.json()["type"] == "Feature"
            assert (await c.get("/trails/nope/geojson")).status_code == 404
            health = (await c.get("/health")).json()
            assert sorted(health["mcp_servers"]) == ["alerts", "gis", "knowledge", "weather"]
    set_scenario("normal")
