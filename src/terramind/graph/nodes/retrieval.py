"""Hybrid Agentic RAG: researcher (spatial + semantic via MCP) and grader (Haiku relevance)."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime
from pydantic import ValidationError

from terramind.graph.nodes.common import emit, error, prompt, to_json
from terramind.graph.state import TerraMindContext, TerraMindState
from terramind.llm.caching import cached_system
from terramind.llm.client import LLMError
from terramind.mcp_client.client import ToolGateway
from terramind.schemas import EvidenceChunk, GradingResult, TrailCandidate, TrailType, TripRequest
from terramind.schemas.tool_results import EvidenceResult, TrailDetails, TrailSearchResult

RRF_K = 60
TYPE_TO_TAG: dict[TrailType, str] = {
    TrailType.WATER: "water",
    TrailType.VIEWPOINT: "viewpoint",
    TrailType.HISTORY: "history",
    TrailType.FAMILY: "family",
}


def search_args(req: TripRequest, exclude: list[str], relaxed: bool) -> dict[str, Any]:
    """Map the structured request to gis.search_trails arguments. Round 2 relaxes soft filters."""
    pace = req.group.pace_kmh
    max_km = req.max_length_km
    if max_km is None and req.max_duration_hours:
        max_km = round(req.max_duration_hours * pace * 0.75, 1)  # leave time for stops
    target_km = max_km * 0.7 if max_km else None
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
        "target_km": target_km,
        "limit": 20 if relaxed else 12,
    }


def fuse_candidates(trails: list[TrailCandidate], chunks: list[EvidenceChunk]) -> list[TrailCandidate]:
    """Reciprocal Rank Fusion of the spatial ranking and the best semantic rank per trail."""
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


async def _pinned_trails(
    tools: ToolGateway, named: list[str], exclude: list[str], found: list[TrailCandidate]
) -> list[TrailCandidate]:
    """Trails the user named explicitly go first, even when the spatial filters missed them.
    A named trail that was vetoed (in `exclude`) is not brought back."""
    by_id = {c.trail_id: c for c in found}
    pinned: list[TrailCandidate] = []
    for tid in named:
        if tid in exclude:
            continue
        trail = by_id.get(tid)
        if trail is None:
            try:
                trail = TrailDetails.model_validate(
                    await tools.call("gis", "get_trail_details", {"trail_id": tid})
                ).trail
            except ValidationError:
                trail = None
        if trail is not None:
            pinned.append(trail.model_copy(update={"fused_score": 1.0}))
    return pinned


async def researcher(state: TerraMindState, runtime: Runtime[TerraMindContext]) -> dict[str, Any]:
    tools = runtime.context.tools
    req = state["request"]
    rounds = state.get("research_rounds", 0)
    relaxed = rounds >= 1 and not state.get("candidates")
    profile = state.get("profile")
    disliked = profile.disliked if profile is not None else []
    exclude = list(dict.fromkeys([*state.get("rejected_trail_ids", []), *disliked]))
    query = state.get("rewritten_query") or req.semantic_query
    emit(runtime, "node", node="researcher", status="start", round=rounds + 1, relaxed=relaxed, query=query)

    errors = []
    raw = await tools.call("gis", "search_trails", search_args(req, exclude, relaxed))
    try:
        spatial = TrailSearchResult.model_validate(raw)
    except ValidationError as exc:
        spatial = TrailSearchResult(status="error", detail=str(exc)[:200])
    if spatial.status in ("error", "unavailable"):
        errors.append(error("researcher", "tool_error", f"search_trails: {spatial.detail}"))

    ids = [t.trail_id for t in spatial.trails]
    raw_ev = await tools.call(
        "knowledge", "hybrid_search", {"query": query, "trail_ids": ids or None, "k": 12}
    )
    try:
        evidence = EvidenceResult.model_validate(raw_ev)
    except ValidationError as exc:
        evidence = EvidenceResult(status="error", detail=str(exc)[:200])
    if evidence.status in ("error", "unavailable"):
        errors.append(error("researcher", "tool_error", f"hybrid_search: {evidence.detail}"))

    candidates = fuse_candidates(spatial.trails, evidence.chunks)
    pinned = await _pinned_trails(tools, state.get("named_trail_ids", []), exclude, candidates)
    if pinned:
        pinned_ids = {c.trail_id for c in pinned}
        candidates = [*pinned, *(c for c in candidates if c.trail_id not in pinned_ids)]
    emit(
        runtime,
        "candidates",
        node="researcher",
        trails=[
            {"trail_id": c.trail_id, "name": c.name, "km": c.length_km, "score": c.fused_score}
            for c in candidates[:6]
        ],
    )
    update: dict[str, Any] = {
        "candidates": candidates,
        "retrieved": evidence.chunks,
        "research_rounds": rounds + 1,
        "rewritten_query": None,
    }
    if errors:
        update["errors"] = errors
    return update


async def grader(state: TerraMindState, runtime: Runtime[TerraMindContext]) -> dict[str, Any]:
    retrieved = state.get("retrieved", [])
    emit(runtime, "node", node="grader", status="start", chunks=len(retrieved))
    if not retrieved:
        return {"evidence": []}
    req = state["request"]
    payload = [{"chunk_id": c.chunk_id, "trail_id": c.trail_id, "text": c.text} for c in retrieved]
    try:
        result = await runtime.context.llm.structured(
            "grader",
            GradingResult,
            [
                cached_system(prompt("grader")),
                HumanMessage(content=f"REQUEST: {to_json(req)}\n\nCHUNKS: {to_json(payload)}"),
            ],
        )
    except LLMError as exc:
        # Fail open for relevance (the safety layer does not depend on it), but record it.
        return {"evidence": retrieved, "errors": [error("grader", "llm_error", exc)]}
    keep = {g.chunk_id for g in result.grades if g.relevant}
    evidence = [c for c in retrieved if c.chunk_id in keep]
    emit(runtime, "graded", node="grader", kept=len(evidence), total=len(retrieved))
    rewrite = result.rewritten_query if len(evidence) < runtime.context.settings.min_relevant_chunks else None
    return {"evidence": evidence, "rewritten_query": rewrite}


__all__ = ["fuse_candidates", "grader", "researcher", "search_args"]
