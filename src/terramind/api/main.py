"""FastAPI app: SSE chat endpoint, GeoJSON for the map, and the built TypeScript frontend.

Run locally with `uv run python -m scripts.dev` (all services) or `uv run python -m scripts.run_api`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from ingestion import alerts_ingest
from terramind.api.sse import stream_turn
from terramind.config import get_settings
from terramind.runtime import AppRuntime, open_runtime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
FRONTEND = Path(__file__).resolve().parents[3] / "web" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with open_runtime() as rt:
        app.state.rt = rt
        yield


app = FastAPI(title="TerraMind", version="0.1.0", lifespan=lifespan)


def _rt(request: Request) -> AppRuntime:
    rt: AppRuntime = request.app.state.rt
    return rt


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


@app.post("/chat/stream")
async def chat_stream(body: ChatRequest, request: Request) -> EventSourceResponse:
    """Run the five agents on one message and stream their progress as Server-Sent Events."""
    rt = _rt(request)

    async def events() -> AsyncIterator[dict[str, str]]:
        async for event in stream_turn(rt, body.message):
            if await request.is_disconnected():
                break  # the user pressed stop or left: stop the run instead of burning tokens
            yield event

    return EventSourceResponse(events(), ping=15)


@app.get("/trails/{trail_id}/geojson")
async def trail_geojson(trail_id: str, request: Request) -> dict[str, Any]:
    result = await _rt(request).gateway.call("gis", "get_trail_geojson", {"trail_id": trail_id})
    if result.get("status") != "ok":
        raise HTTPException(404 if result.get("status") == "empty" else 503, result.get("detail"))
    feature: dict[str, Any] = result["feature"]
    return feature


@app.get("/scenarios")
async def scenarios() -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=get_settings().mock_alerts_url, timeout=3) as c:
        try:
            listing, current = await c.get("/scenarios"), await c.get("/scenarios/current")
            return {"scenarios": listing.json(), "current": current.json()["name"]}
        except httpx.HTTPError as exc:
            raise HTTPException(503, f"mock alerts API unreachable: {exc}") from exc


@app.post("/scenarios/{name}")
async def set_scenario(name: str) -> dict[str, Any]:
    """Demo control: switch the Mock Alerts API scenario (closures, floods, crowding).

    The alerts server reads stored data, so a real switch triggers an alerts pipeline run right
    away instead of waiting for the schedule. The UI re-sends the selection before every message,
    so an unchanged scenario returns immediately.
    """
    s = get_settings()
    async with httpx.AsyncClient(base_url=s.mock_alerts_url, timeout=3) as c:
        try:
            before = (await c.get("/scenarios/current")).json().get("name")
            resp = await c.post(f"/scenarios/{name}")
        except httpx.HTTPError as exc:
            raise HTTPException(503, f"mock alerts API unreachable: {exc}") from exc
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, resp.text)
    data: dict[str, Any] = resp.json()
    if before == name:
        return {**data, "ingested": False}
    try:
        async with asyncio.timeout(s.ingest_job_timeout_s):
            rows = await alerts_ingest.run(s.database_url, "scenario")
    except Exception as exc:  # the switch happened; report that the stored data did not follow yet
        logging.getLogger("terramind.api").warning("alerts ingestion after scenario switch failed: %s", exc)
        return {**data, "ingested": False, "detail": f"{type(exc).__name__}: {exc}"[:300]}
    return {**data, "ingested": True, "rows": rows}


@app.get("/health")
async def health(request: Request) -> dict[str, Any]:
    rt = _rt(request)
    return {
        "status": "ok",
        "mcp_servers": await rt.gateway.ensure_connected(),
        "llm": type(rt.context.llm).__name__,
    }


# The TypeScript frontend (web/) is built into web/dist by `npm run build`. In development,
# `npm run dev` serves it on port 5174 instead and proxies API calls here.
if (FRONTEND / "index.html").is_file():
    app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")
else:

    @app.get("/")
    async def index() -> dict[str, str]:
        return {"detail": "frontend not built: run `npm run build` in web/, or `npm run dev` there"}
