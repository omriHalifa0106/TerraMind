"""Agent 2 - catalog search (Hybrid RAG).

Uses tool calling to run two searches in parallel:
  - gis.search_trails       a PostGIS query: trails in the region that fit length/difficulty/tags
  - knowledge.hybrid_search a RAG query over trail descriptions and hikers' tips (pgvector + full text)
Claude (Haiku) chooses the arguments of both calls in one turn; the code executes them together
with asyncio.gather. The two rankings are then fused with Reciprocal Rank Fusion (RRF): a trail
that is both a good spatial match and well described for this kind of trip ranks first.

Safety-relevant arguments are never left to the model: trails vetoed by agent 3 are always
excluded, and if the model does not ask for both searches the code builds the calls itself.
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime
from pydantic import ValidationError

from terramind.graph.nodes.common import emit, error, prompt, to_json
from terramind.graph.state import TerraMindContext, TerraMindState, rejected_ids
from terramind.llm.caching import cached_system
from terramind.llm.client import LLMError
from terramind.mcp_client.client import Server, ToolGateway
from terramind.schemas import EvidenceChunk, TrailCandidate, TrailType, TripRequest
from terramind.schemas.tool_results import EvidenceResult, TrailDetails, TrailSearchResult

RRF_K = 60
EVIDENCE_K = 20
TOOLS: dict[str, Server] = {"search_trails": "gis", "hybrid_search": "knowledge"}
TYPE_TO_TAG: dict[TrailType, str] = {
    TrailType.WATER: "water",
    TrailType.VIEWPOINT: "viewpoint",
    TrailType.HISTORY: "history",
    TrailType.FAMILY: "family",
}


def search_args(req: TripRequest, exclude: list[str], relaxed: bool) -> dict[str, Any]:
    """The PostGIS search the code would run by itself (fallback). Round 2 relaxes soft filters."""
    pace = req.group.pace_kmh
    max_km = req.max_length_km
    if max_km is None and req.max_duration_hours:
        max_km = round(req.max_duration_hours * pace * 0.75, 1)  # leave time for stops
    near_lat, near_lon = req.origin if req.origin else (None, None)
    return {
        "region": req.region,
        "difficulty": req.difficulty.value if req.difficulty else None,
        "max_km": max_km * (1.25 if relaxed else 1.0) if max_km else None,
        "tags": [] if relaxed else [TYPE_TO_TAG[t] for t in req.trail_types if t in TYPE_TO_TAG],
        "loop_only": (TrailType.LOOP in req.trail_types) and not relaxed,
        "exclude_ids": exclude,
        "near_lat": near_lat,
        "near_lon": near_lon,
        "max_distance_km": req.max_drive_minutes * 0.8 if req.max_drive_minutes and req.origin else None,
        "target_km": max_km * 0.7 if max_km else None,
        "limit": 20 if relaxed else 12,
    }


def _planned_calls(
    model_calls: list[dict[str, Any]], req: TripRequest, exclude: list[str], relaxed: bool, day_cap_km: float
) -> list[tuple[str, dict[str, Any]]]:
    """The model's tool calls, with the guards applied: vetoed trails always excluded, and never a
    multi-day trail for a day hike (max_km capped, whatever the model asked for)."""
    by_name = {c["name"]: dict(c["args"]) for c in model_calls if c.get("name") in TOOLS}
    spatial = by_name.get("search_trails") or search_args(req, exclude, relaxed)
    spatial["exclude_ids"] = list(dict.fromkeys([*(spatial.get("exclude_ids") or []), *exclude]))
    spatial["max_km"] = min(spatial.get("max_km") or day_cap_km, day_cap_km)
    semantic = by_name.get("hybrid_search") or {"query": req.semantic_query}
    semantic["k"] = EVIDENCE_K
    return [("search_trails", spatial), ("hybrid_search", semantic)]


def fuse_candidates(trails: list[TrailCandidate], chunks: list[EvidenceChunk]) -> list[TrailCandidate]:
    """Reciprocal Rank Fusion: score = 1/(60 + spatial rank) + 1/(60 + best semantic rank)."""
    semantic_rank: dict[str, int] = {}
    for rank, chunk in enumerate(chunks, start=1):
        if chunk.trail_id and chunk.trail_id not in semantic_rank:
            semantic_rank[chunk.trail_id] = rank
    fused: list[TrailCandidate] = []
    for t in trails:
        sem = semantic_rank.get(t.trail_id)
        score = 1 / (RRF_K + (t.spatial_rank or 100)) + (1 / (RRF_K + sem) if sem else 0.0)
        fused.append(t.model_copy(update={"semantic_rank": sem, "fused_score": round(score, 5)}))
    return sorted(fused, key=lambda c: c.fused_score, reverse=True)


async def _named_first(
    tools: ToolGateway, named: list[str], exclude: list[str], found: list[TrailCandidate]
) -> list[TrailCandidate]:
    """Trails the user named go first, even if the spatial filters missed them (unless vetoed)."""
    by_id = {c.trail_id: c for c in found}
    pinned: list[TrailCandidate] = []
    for tid in named:
        if tid in exclude:
            continue
        trail = by_id.get(tid)
        if trail is None:
            try:
                raw = await tools.call("gis", "get_trail_details", {"trail_id": tid})
                trail = TrailDetails.model_validate(raw).trail
            except ValidationError:
                trail = None
        if trail is not None:
            pinned.append(trail.model_copy(update={"fused_score": 1.0}))
    ids = {c.trail_id for c in pinned}
    return [*pinned, *(c for c in found if c.trail_id not in ids)]


async def catalog_search(state: TerraMindState, runtime: Runtime[TerraMindContext]) -> dict[str, Any]:
    ctx = runtime.context
    req = state["request"]
    rounds = state.get("search_rounds", 0)
    relaxed = rounds >= 1  # a second round means agent 3 vetoed everything: widen the search
    exclude = rejected_ids(state)
    emit(runtime, "node", node="catalog_search", status="start", round=rounds + 1, relaxed=relaxed)

    errors = []
    tools = [
        *ctx.tools.langchain_tools("gis", ["search_trails"]),
        *ctx.tools.langchain_tools("knowledge", ["hybrid_search"]),
    ]
    try:
        model_calls = await ctx.llm.tool_calls(
            "catalog_search",
            [
                cached_system(prompt("catalog_search")),
                HumanMessage(content=f"REQUEST: {to_json(req)}\nEXCLUDE_IDS: {exclude}\nRELAXED: {relaxed}"),
            ],
            tools,
        )
    except LLMError as exc:
        model_calls = []
        errors.append(error("catalog_search", "llm_error", exc))
    calls = _planned_calls(model_calls, req, exclude, relaxed, ctx.settings.max_day_hike_km)
    emit(runtime, "tool_calls", node="catalog_search", calls=[{"tool": n, "args": a} for n, a in calls])

    # The two searches are independent, so they run at the same time.
    raw_spatial, raw_semantic = await asyncio.gather(*(ctx.tools.call(TOOLS[n], n, a) for n, a in calls))
    try:
        spatial = TrailSearchResult.model_validate(raw_spatial)
    except ValidationError as exc:
        spatial = TrailSearchResult(status="error", detail=str(exc)[:200])
    try:
        semantic = EvidenceResult.model_validate(raw_semantic)
    except ValidationError as exc:
        semantic = EvidenceResult(status="error", detail=str(exc)[:200])
    for name, res in (("search_trails", spatial), ("hybrid_search", semantic)):
        if res.status in ("error", "unavailable"):
            errors.append(error("catalog_search", "tool_error", f"{name}: {res.detail}"))

    candidates = fuse_candidates(spatial.trails, semantic.chunks)
    candidates = await _named_first(ctx.tools, state.get("named_trail_ids", []), exclude, candidates)
    emit(
        runtime,
        "candidates",
        node="catalog_search",
        trails=[
            {"trail_id": c.trail_id, "name": c.name_he or c.name, "km": c.length_km} for c in candidates[:6]
        ],
        chunks=len(semantic.chunks),
    )
    update: dict[str, Any] = {
        "candidates": candidates,
        "evidence": semantic.chunks,
        "search_rounds": rounds + 1,
    }
    if errors:
        update["errors"] = errors
    return update
