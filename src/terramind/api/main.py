"""FastAPI app: SSE chat endpoint, GeoJSON for the map, static Leaflet frontend.

Run locally with `uv run python -m scripts.run_api` (sets the event loop psycopg needs on Windows).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from terramind.api.sse import stream_turn
from terramind.config import get_settings
from terramind.runtime import AppRuntime, open_runtime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
FRONTEND = Path(__file__).resolve().parents[3] / "frontend"


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
    thread_id: str | None = None
    user_id: str = Field(default="demo-user", max_length=100)


@app.post("/chat/stream")
async def chat_stream(body: ChatRequest, request: Request) -> EventSourceResponse:
    rt = _rt(request)
    thread_id = body.thread_id or f"thread-{uuid.uuid4().hex[:12]}"

    async def events() -> AsyncIterator[dict[str, str]]:
        async for event in stream_turn(rt, body.message, thread_id, body.user_id):
            if await request.is_disconnected():
                break  # client left: stop the run instead of burning tokens
            yield event

    return EventSourceResponse(events(), ping=15)


@app.get("/threads/{thread_id}")
async def get_thread(thread_id: str, request: Request) -> dict[str, Any]:
    snapshot = await _rt(request).graph.aget_state({"configurable": {"thread_id": thread_id}})
    values = snapshot.values or {}
    final = values.get("final")
    return {
        "thread_id": thread_id,
        "messages": [{"role": m.type, "content": m.content} for m in values.get("messages", [])],
        "final": final.model_dump(mode="json") if final else None,
    }


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
    """Demo control: switch the Mock Alerts API scenario (closures, floods, crowding)."""
    async with httpx.AsyncClient(base_url=get_settings().mock_alerts_url, timeout=3) as c:
        resp = await c.post(f"/scenarios/{name}")
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, resp.text)
    data: dict[str, Any] = resp.json()
    return data


@app.get("/health")
async def health(request: Request) -> dict[str, Any]:
    rt = _rt(request)
    return {
        "status": "ok",
        "mcp_servers": await rt.gateway.ensure_connected(),
        "llm": type(rt.context.llm).__name__,
        "llm_provider": rt.context.settings.resolved_llm_provider,
        "persistent_memory": rt.persistent,
    }


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(FRONTEND / "index.html")


app.mount("/static", StaticFiles(directory=FRONTEND), name="static")
