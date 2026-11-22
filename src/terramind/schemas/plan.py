from __future__ import annotations

from datetime import time
from typing import Literal

from pydantic import Field

from terramind.schemas.base import Frozen, LatLon
from terramind.schemas.safety import IssueCode

StopKind = Literal[
    "parking",
    "trailhead",
    "poi",
    "viewpoint",
    "water",
    "food",
    "end",
    "coffee",
    "attraction",
    "winery",
    "farm",  # day-plan places before / after the hike
]


class Stop(Frozen):
    order: int = Field(ge=0)
    name: str
    kind: StopKind
    location: LatLon
    arrive_at: time | None = None
    dwell_minutes: int = Field(default=0, ge=0)
    note: str | None = None
    # Day-plan places only (not trail stops):
    place_id: str | None = None
    category: str | None = None  # restaurant, coffee_cart, winery...
    drive_minutes: int | None = None  # drive from the previous stop
    opening_hours: str | None = None  # as mapped in OSM
    open_state: Literal["open", "closed", "unknown"] | None = None  # at arrival, for the whole visit
    kosher: bool | None = None


class RoutePlan(Frozen):
    plan_id: str
    trail_id: str
    title: str
    stops: list[Stop] = Field(min_length=2)
    walk_km: float
    walk_minutes: int
    drive_minutes: int | None = None  # from the user's starting point to the trailhead
    drive_km: float | None = None
    drive_method: Literal["geodesic_factor", "routing_engine"] | None = None
    drive_route: list[LatLon] = Field(default_factory=list)  # for the map
    origin: LatLon | None = None
    origin_label: str | None = None
    depart_at: time | None = None  # when to leave home to reach the trailhead at start_time
    day_end_time: time | None = None  # end of the last place after the hike (None: hike only)
    dropped_places: list[str] = Field(default_factory=list)  # chosen places left out, with why
    start_time: time
    end_time: time
    alternatives_considered: list[str] = Field(default_factory=list)


class TradeOff(Frozen):
    chosen_trail_id: str
    rejected_trail_id: str
    rejected_trail_name: str | None = None
    factor: IssueCode | Literal["DISTANCE", "PREFERENCE_MATCH", "SCENERY"]
    detail: str  # e.g. "rain probability 70% after 14:00 on an exposed ridge vs 10%"


class FinalAnswer(Frozen):
    summary_md: str
    plan: RoutePlan | None  # None => safe_fallback recommended postponing
    tradeoffs: list[TradeOff] = Field(default_factory=list)
    safety_notes: list[str] = Field(default_factory=list)
    tips: list[str] = Field(default_factory=list)
    cited_chunk_ids: list[str] = Field(default_factory=list)  # used by the faithfulness eval


class NodeError(Frozen):
    node: str
    kind: Literal["timeout", "tool_error", "validation", "llm_error"]
    message: str
    recoverable: bool
