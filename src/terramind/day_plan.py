"""The rest of the day around the hike: places before it (on the drive) and after it, timed by code.

Agent 4's model picks which places fit the request; this module decides when, and whether they still
fit: drive time from the previous stop, a visit length per kind of place, opening hours at arrival,
outdoor places before sunset, and the day's length. A place that does not fit is dropped with the
reason, never squeezed in. Pure functions (no I/O): the drive legs are fetched by agent 4 beforehand.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from itertools import pairwise

from terramind.geo import DRIVE_DETOUR, DRIVE_KMH, haversine_km
from terramind.opening_hours import OpenState, is_open, opens_within
from terramind.schemas import LegEstimate, NearbyPlace, RoutePlan, Stop, StopKind, TripRequest
from terramind.schemas.base import LatLon

# Minutes spent at each kind of place.
DWELL: dict[str, int] = {
    "restaurant": 60,
    "cafe": 30,
    "coffee_cart": 20,
    "attraction": 60,
    "museum": 75,
    "winery": 60,
    "brewery": 60,
    "farm": 45,
    "viewpoint": 20,
    "ruins": 30,
    "waterfall": 30,
}
KIND: dict[str, StopKind] = {
    "restaurant": "food",
    "cafe": "coffee",
    "coffee_cart": "coffee",
    "attraction": "attraction",
    "museum": "attraction",
    "winery": "winery",
    "brewery": "winery",
    "farm": "farm",
    "viewpoint": "viewpoint",
    "ruins": "poi",
    "waterfall": "water",
}
OUTDOOR = {"viewpoint", "ruins", "waterfall"}  # pointless after dark
ON_THE_WAY = {"coffee_cart", "cafe"}  # a coffee on the drive, before the hike
FOOD = {"restaurant", "cafe", "coffee_cart"}
MAX_WAIT_FOR_OPENING = 45  # minutes worth waiting for a place that opens soon after we arrive

Legs = dict[
    tuple[str, str], LegEstimate
]  # (from id, to id) -> drive; ids: "origin", "trailhead", "hike_end", poi_id


def split_before_after(
    chosen: list[NearbyPlace], on_way_ids: set[str]
) -> tuple[list[NearbyPlace], list[NearbyPlace]]:
    """Coffee found along the drive goes before the hike; everything else after it."""
    before = [p for p in chosen if p.poi_id in on_way_ids and p.category in ON_THE_WAY]
    return before, [p for p in chosen if p not in before]


def leg_pairs(before: list[NearbyPlace], after: list[NearbyPlace], has_origin: bool) -> list[tuple[str, str]]:
    """The drives agent 4 must fetch for this order of places."""
    pairs: list[tuple[str, str]] = []
    if before and has_origin:
        chain = ["origin", *(p.poi_id for p in before), "trailhead"]
        pairs += list(pairwise(chain))
    chain = ["hike_end", *(p.poi_id for p in after)]
    pairs += list(pairwise(chain))
    return pairs


def _minutes(legs: Legs, a: str, b: str, where: dict[str, LatLon]) -> int:
    if (leg := legs.get((a, b))) is not None:
        return leg.minutes
    # Not fetched (e.g. a place in between was dropped): the same straight-line estimate the gis server uses.
    return math.ceil(haversine_km(where[a], where[b]) * DRIVE_DETOUR / DRIVE_KMH * 60)


def _stop(p: NearbyPlace, arrive: datetime, drive: int, state: OpenState) -> Stop:
    return Stop(
        order=0,
        name=p.name,
        kind=KIND.get(p.category, "poi"),
        location=p.location,
        arrive_at=arrive.time().replace(second=0, microsecond=0),
        dwell_minutes=DWELL.get(p.category, 30),
        place_id=p.poi_id,
        category=p.category,
        drive_minutes=drive,
        opening_hours=p.opening_hours,
        open_state=state,
        kosher=p.kosher,
    )


def add_places(
    plan: RoutePlan,
    before: list[NearbyPlace],
    after: list[NearbyPlace],
    legs: Legs,
    req: TripRequest,
    sunset: datetime | None,
) -> RoutePlan:
    """The hike plan with the day's places added around it (or dropped, with the reason)."""
    day = req.trip_date or date.today()
    where: dict[str, LatLon] = {"trailhead": plan.stops[0].location, "hike_end": plan.stops[-1].location}
    if plan.origin:
        where["origin"] = plan.origin
    where.update({p.poi_id: p.location for p in (*before, *after)})
    dropped: list[str] = []

    def hhmm(t: datetime) -> str:
        return f"{t:%H:%M}"

    # --- before the hike: work backwards from the start agent 3 approved ---
    first: list[Stop] = []
    nxt, t = "trailhead", datetime.combine(day, plan.start_time)
    into_trailhead: int | None = None
    if plan.origin:
        for p in reversed(before):
            drive = _minutes(legs, p.poi_id, nxt, where)
            leave = t - timedelta(minutes=drive)
            arrive = leave - timedelta(minutes=DWELL.get(p.category, 30))
            state = is_open(p.opening_hours, day, arrive.time(), DWELL.get(p.category, 30))
            if state == "closed":
                dropped.append(f"{p.name}: סגור כשנגיע ({hhmm(arrive)})")
                continue
            if nxt == "trailhead":
                into_trailhead = drive
            elif first:
                first[0] = first[0].model_copy(update={"drive_minutes": drive})
            first.insert(0, _stop(p, arrive, 0, state))
            nxt, t = p.poi_id, arrive
    depart_at, drive_total = plan.depart_at, plan.drive_minutes
    if first and plan.origin:
        from_home = _minutes(legs, "origin", first[0].place_id or "", where)
        first[0] = first[0].model_copy(update={"drive_minutes": from_home})
        depart_at = (t - timedelta(minutes=from_home)).time().replace(second=0, microsecond=0)
        drive_total = from_home + sum(s.drive_minutes or 0 for s in first[1:]) + (into_trailhead or 0)

    # --- after the hike: forwards, within the day's hours ---
    day_start = datetime.combine(day, first[0].arrive_at if first and first[0].arrive_at else plan.start_time)
    limit = day_start + timedelta(hours=req.day_hours) if req.day_hours else None
    last: list[Stop] = []
    prev, t = "hike_end", datetime.combine(day, plan.end_time)
    for p in after:
        drive = _minutes(legs, prev, p.poi_id, where)
        arrive = t + timedelta(minutes=drive)
        dwell = DWELL.get(p.category, 30)
        state = is_open(p.opening_hours, day, arrive.time(), dwell)
        waited = None
        if state == "closed" and (
            opens := opens_within(p.opening_hours, day, arrive.time(), dwell, MAX_WAIT_FOR_OPENING)
        ):
            # Opens soon after we arrive: wait for it rather than drop it.
            waited = datetime.combine(day, opens) - arrive
            arrive, state = datetime.combine(day, opens), "open"
        leave = arrive + timedelta(minutes=dwell)
        if state == "closed":
            dropped.append(f"{p.name}: סגור כשנגיע ({hhmm(arrive)})")
        elif p.category in OUTDOOR and sunset is not None and leave > sunset:
            dropped.append(f"{p.name}: אחרי השקיעה ({sunset:%H:%M})")
        elif limit is not None and leave > limit:
            dropped.append(f"{p.name}: לא נכנס ב-{req.day_hours:g} שעות היום")
        else:
            stop = _stop(p, arrive, drive, state)
            if waited:
                stop = stop.model_copy(
                    update={"note": f"נפתח ב-{hhmm(arrive)}: ממתינים {waited.seconds // 60} דק׳"}
                )
            last.append(stop)
            prev, t = p.poi_id, leave

    hike = list(plan.stops)
    if into_trailhead is not None:
        hike[0] = hike[0].model_copy(update={"drive_minutes": into_trailhead})
    stops = [s.model_copy(update={"order": i}) for i, s in enumerate([*first, *hike, *last])]
    return plan.model_copy(
        update={
            "stops": stops,
            "depart_at": depart_at,
            "drive_minutes": drive_total,
            "day_end_time": t.time().replace(second=0, microsecond=0) if last else None,
            "dropped_places": dropped,
        }
    )


def default_places(places: list[NearbyPlace], req: TripRequest, budget_min: int) -> list[NearbyPlace]:
    """When the model picked nothing: one nearest place per kind asked for, or, for a long enough day,
    a meal and one more place. Nearest first (the list comes sorted by distance)."""
    picked: list[NearbyPlace] = []
    kinds = list(req.want_places) or (["restaurant", "winery", "attraction"] if budget_min >= 120 else [])
    for kind in kinds:
        match = next((p for p in places if p.category == kind and p not in picked), None)
        if match is not None:
            picked.append(match)
    return picked[:4]


def kosher_filter(places: list[NearbyPlace], req: TripRequest) -> list[NearbyPlace]:
    """kosher_only: food places must be marked kosher in OSM (unknown is not kosher)."""
    if not req.kosher_only:
        return places
    return [p for p in places if p.category not in FOOD or p.kosher is True]
