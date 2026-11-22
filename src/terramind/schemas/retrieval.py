from __future__ import annotations

from typing import Literal

from pydantic import Field

from terramind.schemas.base import Frozen, LatLon
from terramind.schemas.trip import Difficulty


class TrailCandidate(Frozen):
    trail_id: str
    name: str
    name_he: str | None = None
    region: str
    length_km: float
    elevation_gain_m: float | None = None
    difficulty: Difficulty | None = None
    tags: list[str] = Field(default_factory=list)
    is_loop: bool | None = None
    trailhead: LatLon
    distance_km: float | None = None  # from the request origin, when given
    spatial_rank: int | None = None
    semantic_rank: int | None = None
    fused_score: float = 0.0  # Reciprocal Rank Fusion


class EvidenceChunk(Frozen):
    chunk_id: str
    trail_id: str | None
    text: str
    source: str
    score: float


class PointOfInterest(Frozen):
    poi_id: str
    name: str | None
    category: str
    location: LatLon
    distance_m: float  # from the trailhead: along the trail when along_trail, else straight line
    along_trail: bool = False


class NearbyPlace(Frozen):
    """A place for the rest of the day: food, coffee cart, attraction, winery, viewpoint..."""

    poi_id: str
    name: str
    category: str
    location: LatLon
    distance_km: float  # from the point, or from the route line when searching along a drive
    opening_hours: str | None = None  # OSM opening_hours as mapped; None = unknown
    kosher: bool | None = None  # from diet:kosher; None = unknown
    cuisine: str | None = None
    website: str | None = None


class LegEstimate(Frozen):
    mode: Literal["walk", "drive"]
    distance_km: float
    minutes: int
    method: Literal["geodesic_factor", "routing_engine"] = "geodesic_factor"
    route: list[LatLon] = Field(default_factory=list)  # road geometry (routing engine only)


class ToolStatus(Frozen):
    """Envelope used by every MCP tool so agents can reason about failures."""

    status: Literal["ok", "empty", "error", "unavailable"]
    detail: str | None = None
