"""Agent 4 - route planning: choose the best safe trail and build the day's schedule.

Only trails agent 3 approved reach this agent. For the top few, it fetches the trail's end point and
the points of interest along it (springs, viewpoints, ruins) from PostGIS, in parallel. Claude
(Sonnet) then makes the judgment call: which trail fits the request best, and which stops are worth
it. Everything numeric is computed by code from the map data, never by the model: stop order
(by distance along the trail), walking times at the group's pace, drive time, start and end.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, time, timedelta
from typing import Any

from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime
from pydantic import BaseModel, Field, ValidationError

from terramind.geo import DWELL_MINUTES, MAX_STOPS, WALK_DETOUR, drive_minutes, hike_minutes, walk_minutes
from terramind.geo import TERRAIN_FACTOR as TERRAIN
from terramind.graph.nodes.common import emit, error, prompt, to_json
from terramind.graph.state import TerraMindContext, TerraMindState, approved
from terramind.llm.caching import cached_system
from terramind.llm.client import LLMError
from terramind.schemas import PointOfInterest, RoutePlan, Stop, StopKind, TrailCandidate, TripRequest
from terramind.schemas.tool_results import PoiResult, TrailDetails

TRAILS_TO_COMPARE = 3
POI_BUFFER_M = 300
MAX_POIS_IN_PROMPT = 12
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


class PlanDraft(BaseModel):
    """What the model decides. Times and distances are computed by build_plan, not by the model."""

    trail_id: str
    title: str = Field(max_length=120)
    poi_ids: list[str] = Field(default_factory=list, max_length=MAX_STOPS)
    rationale: str = Field(max_length=600)


def _at(start: datetime, minutes: float) -> time:
    return (start + timedelta(minutes=minutes)).time().replace(second=0, microsecond=0)


def build_plan(
    draft: PlanDraft,
    trail: TrailCandidate,
    pois: list[PointOfInterest],
    trail_end: tuple[float, float] | None,
    req: TripRequest,
    start_time: time,
    alternatives: list[str],
) -> RoutePlan:
    """The deterministic schedule: stops ordered by distance along the trail, times at the group's pace."""
    pace = req.group.pace_kmh
    start = datetime.combine(req.trip_date or date.today(), start_time)

    chosen = {p.poi_id: p for p in pois if p.poi_id in set(draft.poi_ids)} or {
        p.poi_id: p for p in pois if p.category in STOP_CATEGORIES
    }
    ordered = sorted(chosen.values(), key=lambda p: p.distance_m)[:MAX_STOPS]

    name = trail.name_he or trail.name
    stops = [
        Stop(
            order=0,
            name=f"תחילת מסלול {name}" if name.isdigit() else f"תחילת {name}",
            kind="trailhead",
            location=trail.trailhead,
            arrive_at=start.time().replace(second=0, microsecond=0),
        )
    ]
    dwell = 0
    for p in ordered:
        detour = 1.0 if p.along_trail else WALK_DETOUR  # along-trail distance is already the real path
        walked = walk_minutes(p.distance_m / 1000 * detour, pace) * TERRAIN
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
    walking = hike_minutes(trail.length_km, pace)
    end_loc = trail.trailhead if trail.is_loop or trail_end is None else trail_end
    stops.append(
        Stop(
            order=len(stops), name="סיום", kind="end", location=end_loc, arrive_at=_at(start, walking + dwell)
        )
    )
    return RoutePlan(
        plan_id=f"plan-{uuid.uuid4().hex[:10]}",
        trail_id=trail.trail_id,
        title=draft.title,
        stops=stops,
        walk_km=trail.length_km,
        walk_minutes=walking,
        drive_minutes=drive_minutes(req.origin, trail.trailhead) if req.origin else None,
        start_time=start.time().replace(second=0, microsecond=0),
        end_time=_at(start, walking + dwell),
        alternatives_considered=alternatives,
    )


async def _map_data(
    ctx: TerraMindContext, trail_id: str
) -> tuple[tuple[float, float] | None, list[PointOfInterest], list[str]]:
    """The trail's end point and the POIs along it (two PostGIS calls in parallel)."""
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
    """Compact POI list for the prompt: what the model needs to pick stops, nothing more."""
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


async def route_planner(state: TerraMindState, runtime: Runtime[TerraMindContext]) -> dict[str, Any]:
    ctx = runtime.context
    req = state["request"]
    safe = approved(state)[:TRAILS_TO_COMPARE]
    by_id = {c.trail_id: c for c in state.get("candidates", [])}
    top = [by_id[a.trail_id] for a in safe]
    emit(runtime, "node", node="route_planner", status="start", candidates=len(top))

    fetched = await asyncio.gather(*(_map_data(ctx, c.trail_id) for c in top))
    data = {c.trail_id: f for c, f in zip(top, fetched, strict=True)}
    errors = [error("route_planner", "tool_error", p) for f in fetched for p in f[2]]
    # Only tips about the safe candidates (or general tips): text about a vetoed trail must not
    # tempt the model back to it.
    evidence = [
        {"id": e.chunk_id, "trail_id": e.trail_id, "text": e.text}
        for e in state.get("evidence", [])
        if e.trail_id is None or e.trail_id in data
    ][:8]
    named = [t for t in state.get("named_trail_ids", []) if t in data]

    try:
        draft = await ctx.llm.structured(
            "route_planner",
            PlanDraft,
            [
                cached_system(prompt("route_planner")),
                HumanMessage(
                    content=(
                        f"REQUEST: {to_json(req)}\nCANDIDATES: {to_json(top)}\nNAMED BY USER: {named}\n"
                        f"POIS BY TRAIL: {to_json({tid: _poi_brief(f[1]) for tid, f in data.items()})}\n"
                        f"EVIDENCE: {to_json(evidence)}"
                    )
                ),
            ],
        )
    except LLMError as exc:
        draft = PlanDraft(
            trail_id=top[0].trail_id,
            title=top[0].name_he or top[0].name,
            rationale="Highest-ranked safe candidate (planner model unavailable).",
        )
        errors.append(error("route_planner", "llm_error", exc))

    if draft.trail_id not in data:  # the model chose a trail outside the safe set: never trust that
        errors.append(
            error("route_planner", "validation", f"model chose {draft.trail_id!r} outside candidates")
        )
        draft = draft.model_copy(update={"trail_id": top[0].trail_id})
    trail = by_id[draft.trail_id]
    start = next(a.start_time for a in safe if a.trail_id == trail.trail_id)  # the start agent 3 approved
    trail_end, pois, _ = data[trail.trail_id]
    plan = build_plan(draft, trail, pois, trail_end, req, start, [c.trail_id for c in top if c is not trail])

    emit(runtime, "plan", node="route_planner", plan=plan.model_dump(mode="json"), rationale=draft.rationale)
    update: dict[str, Any] = {"plan": plan, "plan_rationale": draft.rationale}
    if errors:
        update["errors"] = errors
    return update
