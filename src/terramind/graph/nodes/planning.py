"""Planner (one Sonnet call over pre-fetched map data) and enrich (deterministic parallel tool calls).

The planner used to run a tool-calling loop (the model asked for POIs, then answered), which cost
2-4 sequential Sonnet round trips per attempt. The data it asked for is always the same, so it is
now fetched up front for the top candidates in parallel and the model decides in a single call.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, time, timedelta
from typing import Any

from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime
from pydantic import BaseModel, Field, ValidationError

from terramind.geo import WALK_DETOUR, drive_minutes, walk_minutes
from terramind.graph.nodes.common import emit, error, prompt, to_json
from terramind.graph.state import TerraMindContext, TerraMindState
from terramind.llm.caching import cached_system
from terramind.llm.client import LLMError
from terramind.safety.rules import T as THRESHOLDS
from terramind.schemas import (
    PointOfInterest,
    RoutePlan,
    Stop,
    StopKind,
    TrailAlert,
    TrailCandidate,
    TrailStatus,
    TripRequest,
    WeatherSnapshot,
)
from terramind.schemas.tool_results import AlertsResult, PoiResult, TrailDetails, TrailStatusResult

DEFAULT_START = time(8, 0)
TERRAIN_FACTOR = 1.15  # real trails are slower than flat walking
DWELL_MINUTES = 15
CATEGORY_LABELS = {
    "spring": "מעיין",
    "waterfall": "מפל",
    "drinking_water": "ברז מי שתייה",
    "viewpoint": "תצפית",
    "ruins": "חורבה",
    "picnic": "חניון פיקניק",
    "parking": "חניה",
}
STOP_CATEGORIES: dict[str, StopKind] = {
    "spring": "water",
    "waterfall": "water",
    "drinking_water": "water",
    "viewpoint": "viewpoint",
    "ruins": "poi",
    "picnic": "food",
}
PLANNER_CANDIDATES = 3
POI_BUFFER_M = 300
MAX_POIS_IN_PROMPT = 12


class PlanDraft(BaseModel):
    """What the LLM decides. All times and distances are computed by code, not by the model."""

    trail_id: str
    title: str = Field(max_length=120)
    start_time: time | None = None
    poi_ids: list[str] = Field(default_factory=list, max_length=5)
    rationale: str = Field(max_length=600)


def _at(start: datetime, minutes: float) -> time:
    return (start + timedelta(minutes=minutes)).time().replace(second=0, microsecond=0)


def build_plan(
    draft: PlanDraft,
    trail: TrailCandidate,
    pois: list[PointOfInterest],
    trail_end: tuple[float, float] | None,
    req: TripRequest,
    shift_minutes: int,
    alternatives: list[str],
) -> RoutePlan:
    """Deterministic schedule: stop times from distance along the trail at the group's pace."""
    pace = req.group.pace_kmh
    base = draft.start_time or req.start_time or DEFAULT_START
    day = datetime.combine(req.trip_date or date.today(), base) - timedelta(minutes=shift_minutes)
    earliest = datetime.combine(day.date(), THRESHOLDS.earliest_start)
    start = max(day, earliest)

    chosen = {p.poi_id: p for p in pois if p.poi_id in set(draft.poi_ids)} or {
        p.poi_id: p for p in pois if p.category in STOP_CATEGORIES
    }
    ordered = sorted(chosen.values(), key=lambda p: p.distance_m)[:5]

    trail_name = trail.name_he or trail.name
    start_name = f"תחילת מסלול {trail_name}" if trail_name.isdigit() else f"תחילת {trail_name}"
    stops = [
        Stop(
            order=0,
            name=start_name,
            kind="trailhead",
            location=trail.trailhead,
            arrive_at=start.time().replace(second=0, microsecond=0),
        )
    ]
    dwell = 0
    for p in ordered:
        detour = 1.0 if p.along_trail else WALK_DETOUR  # along-trail distance is already the real path
        walked = walk_minutes(p.distance_m / 1000 * detour, pace) * TERRAIN_FACTOR
        stops.append(
            Stop(
                order=len(stops),
                name=p.name or CATEGORY_LABELS.get(p.category, p.category),
                kind=STOP_CATEGORIES.get(p.category, "poi"),
                location=p.location,
                arrive_at=_at(start, walked + dwell),
                dwell_minutes=DWELL_MINUTES,
            )
        )
        dwell += DWELL_MINUTES
    total_walk = round(walk_minutes(trail.length_km, pace) * TERRAIN_FACTOR)
    total = total_walk + dwell
    end_loc = trail.trailhead if trail.is_loop or trail_end is None else trail_end
    stops.append(
        Stop(
            order=len(stops),
            name="סיום",
            kind="end",
            location=end_loc,
            arrive_at=_at(start, total),
        )
    )
    return RoutePlan(
        plan_id=f"plan-{uuid.uuid4().hex[:10]}",
        trail_id=trail.trail_id,
        title=draft.title,
        stops=stops,
        walk_km=trail.length_km,
        walk_minutes=total_walk,
        drive_minutes=drive_minutes(req.origin, trail.trailhead) if req.origin else None,
        start_time=start.time().replace(second=0, microsecond=0),
        end_time=_at(start, total),
        alternatives_considered=alternatives,
    )


async def _trail_data(
    ctx: TerraMindContext, trail_id: str
) -> tuple[tuple[float, float] | None, list[PointOfInterest], list[str]]:
    """Trail end point and POIs for one trail (two MCP calls in parallel)."""
    details_raw, pois_raw = await asyncio.gather(
        ctx.tools.call("gis", "get_trail_details", {"trail_id": trail_id}),
        ctx.tools.call("gis", "find_pois_near_trail", {"trail_id": trail_id, "buffer_m": POI_BUFFER_M}),
    )
    problems: list[str] = []
    try:
        trail_end = TrailDetails.model_validate(details_raw).trail_end
    except ValidationError as exc:
        trail_end = None
        problems.append(f"get_trail_details({trail_id}): {str(exc)[:150]}")
    try:
        pois = PoiResult.model_validate(pois_raw).pois
    except ValidationError as exc:
        pois = []
        problems.append(f"find_pois_near_trail({trail_id}): {str(exc)[:150]}")
    return trail_end, pois, problems


def _poi_brief(pois: list[PointOfInterest]) -> list[dict[str, Any]]:
    """Compact POI view for the prompt: what the model needs to pick stops, nothing more."""
    ranked = sorted(pois, key=lambda p: (p.category not in STOP_CATEGORIES, p.distance_m))
    return [
        {
            "poi_id": p.poi_id,
            "name": p.name or CATEGORY_LABELS.get(p.category, p.category),
            "category": p.category,
            "distance_m": round(p.distance_m),
            "along_trail": p.along_trail,
        }
        for p in ranked[:MAX_POIS_IN_PROMPT]
    ]


async def planner(state: TerraMindState, runtime: Runtime[TerraMindContext]) -> dict[str, Any]:
    ctx = runtime.context
    req = state["request"]
    rejected = set(state.get("rejected_trail_ids", []))
    pool = [c for c in state.get("candidates", []) if c.trail_id not in rejected]
    emit(runtime, "node", node="planner", status="start", candidates=len(pool))
    if not pool:
        return {"draft_plan": None}
    top = pool[:PLANNER_CANDIDATES]
    verdict = state.get("verdict")
    guidance = verdict.guidance_for_replan if verdict and not verdict.approved else None
    shift = state.get("start_shift_minutes", 0)

    fetched = await asyncio.gather(*(_trail_data(ctx, c.trail_id) for c in top))
    data = {c.trail_id: f for c, f in zip(top, fetched, strict=True)}
    errors = [error("planner", "tool_error", p) for f in fetched for p in f[2]]

    evidence = [
        {"id": e.chunk_id, "trail_id": e.trail_id, "text": e.text} for e in state.get("evidence", [])
    ][:8]
    pois_by_trail = {tid: _poi_brief(f[1]) for tid, f in data.items()}
    named = [t for t in state.get("named_trail_ids", []) if t not in rejected]
    try:
        draft = await ctx.llm.structured(
            "planner",
            PlanDraft,
            [
                cached_system(prompt("planner")),
                HumanMessage(
                    content=(
                        f"REQUEST: {to_json(req)}\nCANDIDATES: {to_json(top)}\nREJECTED: {sorted(rejected)}\n"
                        f"NAMED BY USER: {named}\n"
                        f"POIS BY TRAIL: {to_json(pois_by_trail)}\n"
                        f"EVIDENCE: {to_json(evidence)}\n"
                        f"SAFETY GUIDANCE: {guidance or 'none'}"
                    )
                ),
            ],
        )
    except LLMError as exc:
        draft = PlanDraft(
            trail_id=top[0].trail_id,
            title=top[0].name_he or top[0].name,
            rationale="Highest-ranked candidate (planner model unavailable).",
        )
        errors.append(error("planner", "llm_error", exc))

    trail = next((c for c in top if c.trail_id == draft.trail_id), None)
    if trail is None:  # the model picked something outside the allowed set: override, never trust it
        errors.append(error("planner", "validation", f"model chose {draft.trail_id!r} outside candidates"))
        trail = top[0]
        draft = draft.model_copy(update={"trail_id": trail.trail_id})

    trail_end, pois, _ = data[trail.trail_id]
    plan = build_plan(draft, trail, pois, trail_end, req, shift, [c.trail_id for c in top if c is not trail])
    emit(runtime, "plan", node="planner", plan=plan.model_dump(mode="json"), rationale=draft.rationale)
    update: dict[str, Any] = {"draft_plan": plan, "plan_rationale": draft.rationale}
    if errors:
        update["errors"] = errors
    return update


async def enrich(state: TerraMindState, runtime: Runtime[TerraMindContext]) -> dict[str, Any]:
    """Fetch weather, daylight, trail status and alerts in parallel. No LLM involved."""
    tools = runtime.context.tools
    plan = state["draft_plan"]
    assert plan is not None
    req = state["request"]
    trail = next(c for c in state.get("candidates", []) if c.trail_id == plan.trail_id)
    on = (req.trip_date or date.today()).isoformat()
    emit(runtime, "node", node="enrich", status="start", trail_id=plan.trail_id)
    lat, lon = trail.trailhead
    # Closures/alerts are matched by name; OSM keeps the route number and the Hebrew name in
    # different fields ("מסלול 2224" / "ליד נחל כזיב"), so check against all of them.
    names = " / ".join(dict.fromkeys(n for n in (trail.name, trail.name_he) if n))
    w_raw, s_raw, a_raw = await asyncio.gather(
        tools.call(
            "weather", "get_forecast", {"trail_id": trail.trail_id, "lat": lat, "lon": lon, "for_date": on}
        ),
        tools.call(
            "alerts",
            "get_trail_status",
            {"trail_id": trail.trail_id, "trail_name": names, "for_date": on},
        ),
        tools.call(
            "alerts",
            "list_active_alerts",
            {"region": req.region, "trail_id": trail.trail_id, "trail_name": names, "for_date": on},
        ),
    )
    weather: WeatherSnapshot | None
    try:
        weather = WeatherSnapshot.model_validate(w_raw)
    except ValidationError:
        weather = None
    status: TrailStatus | None
    try:
        status = TrailStatusResult.model_validate(s_raw).trail
    except ValidationError:
        status = None
    alerts: list[TrailAlert]
    try:
        alerts = AlertsResult.model_validate(a_raw).alerts
    except ValidationError:
        alerts = []
    emit(
        runtime,
        "conditions",
        node="enrich",
        weather=weather.model_dump(mode="json") if weather else None,
        status=status.model_dump(mode="json") if status else None,
        alerts=[a.model_dump(mode="json") for a in alerts],
    )
    return {"weather": weather, "trail_status": status, "alerts": alerts}
