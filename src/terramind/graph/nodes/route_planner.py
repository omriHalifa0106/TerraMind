"""Agent 4 - route planning: choose the best safe trail and build the day's schedule.

Only trails agent 3 approved reach this agent. For the top few, it fetches the trail's end point and
the points of interest along it (springs, viewpoints, ruins) from PostGIS, in parallel. Claude
(Sonnet) then makes the judgment call: which trail fits the request best, and which stops are worth
it. Everything numeric is computed by code from the map data, never by the model: stop order
(by distance along the trail), walking times at the group's pace, start and end. When the user set
a starting point, the drive to the trailhead comes from real roads (OSRM, via the gis server's
estimate_leg), and the plan says when to leave home.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import Any

from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime
from pydantic import BaseModel, Field, ValidationError

from terramind.day_plan import (
    ON_THE_WAY,
    Legs,
    add_places,
    best_start,
    default_places,
    kosher_filter,
    leg_pairs,
    split_before_after,
)
from terramind.geo import (
    DWELL_MINUTES,
    MAX_STOPS,
    WALK_DETOUR,
    haversine_km,
    hike_minutes,
    max_outing_minutes,
    walk_minutes,
)
from terramind.geo import TERRAIN_FACTOR as TERRAIN
from terramind.graph.nodes.common import emit, error, prompt, to_json
from terramind.graph.state import TerraMindContext, TerraMindState, approved
from terramind.llm.caching import cached_system
from terramind.llm.client import LLMError
from terramind.safety.rules import evaluate
from terramind.schemas import (
    LegEstimate,
    NearbyPlace,
    NodeError,
    PointOfInterest,
    RoutePlan,
    Severity,
    Stop,
    StopKind,
    TrailCandidate,
    TripRequest,
)
from terramind.schemas.base import LatLon
from terramind.schemas.tool_results import NearbyPlacesResult, PoiResult, TrailDetails

TRAILS_TO_COMPARE = 3
POI_BUFFER_M = 300
MAX_POIS_IN_PROMPT = 12
MAX_PLACES = 4  # day-plan places around the hike
PLACES_RADIUS_KM = 12  # around the trailhead
ON_THE_WAY_RADIUS_KM = 2  # around the drive from home
SAME_PLACE_KM = 0.3  # a place this close to a trail stop of the same kind is that stop
PLACE_KINDS_BY_DEFAULT = [
    "restaurant",
    "cafe",
    "coffee_cart",
    "attraction",
    "museum",
    "winery",
    "brewery",
    "farm",
]
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
    # Required (may be empty): in json_schema mode Claude leaves optional fields out.
    poi_ids: list[str] = Field(max_length=MAX_STOPS)
    place_ids: list[str] = Field(max_length=MAX_PLACES)  # day-plan places, in order
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
    drive: LegEstimate | None = None,
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
        drive_minutes=drive.minutes if drive else None,
        drive_km=drive.distance_km if drive else None,
        drive_method=drive.method if drive else None,
        drive_route=drive.route if drive else [],
        origin=req.origin,
        origin_label=req.origin_label,
        depart_at=_at(start, -drive.minutes) if drive else None,
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


async def _drive_leg(
    ctx: TerraMindContext, req: TripRequest, trail: TrailCandidate, errors: list[NodeError]
) -> LegEstimate | None:
    """Road drive from the user's starting point to the trailhead (OSRM through the gis server)."""
    if req.origin is None:
        return None
    raw = await ctx.tools.call(
        "gis",
        "estimate_leg",
        {
            "from_lat": req.origin[0],
            "from_lon": req.origin[1],
            "to_lat": trail.trailhead[0],
            "to_lon": trail.trailhead[1],
            "mode": "drive",
        },
    )
    try:
        return LegEstimate.model_validate(raw)
    except ValidationError as exc:
        errors.append(error("route_planner", "tool_error", f"estimate_leg: {str(exc)[:150]}"))
        return None


async def _places(
    ctx: TerraMindContext, req: TripRequest, trail: TrailCandidate, drive: LegEstimate | None
) -> tuple[list[NearbyPlace], set[str]]:
    """Places for the day around this trail, and which of them are on the drive from home."""
    kinds = list(req.want_places) or PLACE_KINDS_BY_DEFAULT
    near_args = {
        "lat": trail.trailhead[0],
        "lon": trail.trailhead[1],
        "radius_km": PLACES_RADIUS_KM,
        "categories": kinds,
        "limit": 15,
    }
    calls = [ctx.tools.call("gis", "find_places_near", near_args)]
    if drive is not None and len(drive.route) >= 2:
        way_args = {
            "route": drive.route,
            "radius_km": ON_THE_WAY_RADIUS_KM,
            # Coffee only: other kinds near the road would crowd it out of the limit, and the
            # rest of the day's places belong near the trail.
            "categories": sorted(ON_THE_WAY),
            "limit": 8,
        }
        calls.append(ctx.tools.call("gis", "find_places_near", way_args))
    found: list[list[NearbyPlace]] = []
    for raw in await asyncio.gather(*calls):
        try:
            found.append(NearbyPlacesResult.model_validate(raw).places)
        except ValidationError:
            found.append([])
    near, on_way = found[0], (found[1] if len(found) > 1 else [])
    way_ids = {p.poi_id for p in on_way}
    places = kosher_filter([*on_way, *(p for p in near if p.poi_id not in way_ids)], req)
    return places, way_ids


def _current_places(previous_plan: RoutePlan) -> tuple[list[NearbyPlace], set[str]]:
    """The places of the plan being edited, rebuilt from the plan itself, so "keep them" never depends
    on what a new search happens to return. Places before the hike were on the way."""
    start = previous_plan.start_time
    places: list[NearbyPlace] = []
    before: set[str] = set()
    for s in previous_plan.stops:
        if not s.place_id or not s.category:
            continue
        places.append(
            NearbyPlace(
                poi_id=s.place_id,
                name=s.name,
                category=s.category,
                location=s.location,
                distance_km=0.0,
                opening_hours=s.opening_hours,
                kosher=s.kosher,
            )
        )
        if s.arrive_at is not None and s.arrive_at < start:
            before.add(s.place_id)
    return places, before


def _not_on_trail(places: list[NearbyPlace], pois: list[PointOfInterest]) -> list[NearbyPlace]:
    """Drop places that are already stops of the hike itself (the same waterfall found twice)."""

    def same(p: NearbyPlace, q: PointOfInterest) -> bool:
        if q.name and q.name == p.name:
            return True
        return q.category == p.category and haversine_km(p.location, q.location) < SAME_PLACE_KM

    return [p for p in places if not any(same(p, q) for q in pois)]


async def _legs(ctx: TerraMindContext, pairs: list[tuple[str, str]], where: dict[str, LatLon]) -> Legs:
    """The drives between consecutive day-plan stops (OSRM through the gis server), in parallel."""
    pairs = [(a, b) for a, b in pairs if a in where and b in where]
    raws = await asyncio.gather(
        *(
            ctx.tools.call(
                "gis",
                "estimate_leg",
                {
                    "from_lat": where[a][0],
                    "from_lon": where[a][1],
                    "to_lat": where[b][0],
                    "to_lon": where[b][1],
                    "mode": "drive",
                },
            )
            for a, b in pairs
        )
    )
    legs: Legs = {}
    for pair, raw in zip(pairs, raws, strict=True):
        # A leg that fails validation is left out: add_places falls back to the straight-line estimate.
        with contextlib.suppress(ValidationError):
            legs[pair] = LegEstimate.model_validate(raw)
    return legs


def _place_brief(places: list[NearbyPlace], on_way: set[str]) -> list[dict[str, Any]]:
    """Compact place list for the prompt."""
    return [
        {
            "place_id": p.poi_id,
            "name": p.name,
            "category": p.category,
            "distance_km": p.distance_km,
            "on_the_way": p.poi_id in on_way,
            "opening_hours": p.opening_hours or "unknown",
            "kosher": p.kosher,
        }
        for p in places
    ]


async def _day_builder(
    ctx: TerraMindContext,
    hike_at: Callable[[time], RoutePlan],
    start: time,
    draft: PlanDraft,
    req: TripRequest,
    found: tuple[list[NearbyPlace], set[str]] | None,
    budget_min: int,
    sunset: datetime | None,
    keep_ids: list[str],
) -> Callable[[time], RoutePlan]:
    """The day at a given hike start: the places the model chose (or sensible defaults) around the hike,
    timed by day_plan. Drive legs are fetched once here; the returned function is pure.
    keep_ids: the places of the plan being edited, kept when the model chose none."""
    plan = hike_at(start)
    places, on_way = found or ([], set())
    by_place = {p.poi_id: p for p in places}
    chosen = [by_place[i] for i in draft.place_ids if i in by_place]  # ids outside the list are ignored
    if not chosen:
        kept = [by_place[i] for i in keep_ids if i in by_place]
        have = {p.category for p in kept}
        missing = req.model_copy(update={"want_places": [k for k in req.want_places if k not in have]})
        extra = default_places(places, missing, budget_min) if missing.want_places or not kept else []
        chosen = [*kept, *(p for p in extra if p not in kept)]
    if not chosen:
        return hike_at
    before, after = split_before_after(chosen[:MAX_PLACES], on_way)
    where: dict[str, LatLon] = {"trailhead": plan.stops[0].location, "hike_end": plan.stops[-1].location}
    if plan.origin:
        where["origin"] = plan.origin
    where.update({p.poi_id: p.location for p in chosen})
    legs = await _legs(ctx, leg_pairs(before, after, plan.origin is not None), where)

    def day_at(at: time) -> RoutePlan:
        return add_places(hike_at(at), before, after, legs, req, sunset)

    return day_at


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

    # The drive from home per candidate (also used to find a coffee on the way).
    drives = await asyncio.gather(*(_drive_leg(ctx, req, c, errors) for c in top))
    drive_by = {c.trail_id: d for c, d in zip(top, drives, strict=True)}
    # A whole day out (hours given, or places asked for): places around each candidate.
    wants_day = bool(req.day_hours or req.want_places)
    places_by: dict[str, tuple[list[NearbyPlace], set[str]]] = {}
    if wants_day:
        found = await asyncio.gather(*(_places(ctx, req, c, drive_by[c.trail_id]) for c in top))
        places_by = {
            c.trail_id: (_not_on_trail(f[0], data[c.trail_id][1]), f[1])
            for c, f in zip(top, found, strict=True)
        }
    # An edit of the current plan ("add a restaurant") keeps what the user did not ask to change.
    previous = state.get("previous")
    if wants_day and state.get("scope") == "replan" and previous and previous.plan:
        kept, kept_before = _current_places(previous.plan)
        if previous.plan.trail_id in places_by:
            found_places, way = places_by[previous.plan.trail_id]
            known = {p.poi_id for p in found_places}
            places_by[previous.plan.trail_id] = (
                [*(p for p in kept if p.poi_id not in known), *found_places],
                way | kept_before,
            )
    current_plan = (
        {
            "trail_id": previous.plan.trail_id,
            "place_ids": [s.place_id for s in previous.plan.stops if s.place_id],
        }
        if state.get("scope") == "replan" and previous and previous.plan
        else None
    )
    budgets = {
        c.trail_id: int(req.day_hours * 60 - hike_minutes(c.length_km, req.group.pace_kmh))
        for c in top
        if req.day_hours
    }
    place_lists = {t: _place_brief(*f) for t, f in places_by.items()}
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
                        f"PLACES BY TRAIL: {to_json(place_lists)}\n"
                        f"MINUTES LEFT FOR PLACES (day length minus the hike): {budgets or 'not given'}\n"
                        f"CURRENT PLAN (an edit of it; keep its places unless the message changes them): "
                        f"{to_json(current_plan) if current_plan else 'none'}\n"
                        f"USER MESSAGE: {state['messages'][-1].content if state.get('messages') else ''}\n"
                        f"EVIDENCE: {to_json(evidence)}"
                    )
                ),
            ],
        )
    except LLMError as exc:
        draft = PlanDraft(
            trail_id=top[0].trail_id,
            title=top[0].name_he or top[0].name,
            poi_ids=[],
            place_ids=[],
            rationale="Highest-ranked safe candidate (planner model unavailable).",
        )
        errors.append(error("route_planner", "llm_error", exc))

    if draft.trail_id not in data:  # the model chose a trail outside the safe set: never trust that
        errors.append(
            error("route_planner", "validation", f"model chose {draft.trail_id!r} outside candidates")
        )
        draft = draft.model_copy(update={"trail_id": top[0].trail_id})
    trail = by_id[draft.trail_id]
    chosen = next(a for a in safe if a.trail_id == trail.trail_id)
    start = chosen.start_time  # the start agent 3 approved
    trail_end, pois, _ = data[trail.trail_id]
    others = [c.trail_id for c in top if c is not trail]

    def hike_at(at: time) -> RoutePlan:
        return build_plan(draft, trail, pois, trail_end, req, at, others, drive_by[trail.trail_id])

    weather = chosen.weather
    outing = max_outing_minutes(trail.length_km, req.group.pace_kmh)

    def safe_at(at: time) -> bool:
        # The same deterministic rules agent 3 used, with the same weather/status/alerts.
        end = (datetime.combine(date.min, at) + timedelta(minutes=outing)).time()
        issues = evaluate(trail, req, weather, chosen.trail_status, chosen.alerts, at, end)
        return not any(i.severity is Severity.CRITICAL for i in issues)

    day_at: Callable[[time], RoutePlan] = hike_at
    if wants_day:
        day_at = await _day_builder(
            ctx,
            hike_at,
            start,
            draft,
            req,
            places_by.get(trail.trail_id),
            budgets.get(trail.trail_id, 0),
            weather.sunset if weather else None,
            list(current_plan["place_ids"])
            if current_plan and current_plan["trail_id"] == trail.trail_id
            else [],
        )
    # The hike's start moves only when the user did not fix it: to after the drive when they said when
    # they leave home, and later still if that keeps more places open.
    drive = drive_by[trail.trail_id]
    earliest = start
    if req.depart_time is not None and drive is not None:
        earliest = (datetime.combine(date.min, req.depart_time) + timedelta(minutes=drive.minutes)).time()
    if req.start_time is None and (wants_day or req.depart_time is not None):
        plan = best_start(day_at, earliest, safe_at, start, req.depart_time)
    else:
        plan = day_at(start)

    emit(runtime, "plan", node="route_planner", plan=plan.model_dump(mode="json"), rationale=draft.rationale)
    update: dict[str, Any] = {"plan": plan, "plan_rationale": draft.rationale}
    if errors:
        update["errors"] = errors
    return update
