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


class RelevanceGrade(Frozen):
    chunk_id: str
    relevant: bool
    reason: str = Field(max_length=300)


class GradingResult(Frozen):
    grades: list[RelevanceGrade]
    rewritten_query: str | None = Field(
        default=None, description="A better search query, only if most chunks were irrelevant"
    )


class PointOfInterest(Frozen):
    poi_id: str
    name: str | None
    category: str
    location: LatLon
    distance_m: float  # from the trailhead: along the trail when along_trail, else straight line
    along_trail: bool = False


class LegEstimate(Frozen):
    mode: Literal["walk", "drive"]
    distance_km: float
    minutes: int
    method: Literal["geodesic_factor", "routing_engine"] = "geodesic_factor"


class ToolStatus(Frozen):
    """Envelope used by every MCP tool so agents can reason about failures."""

    status: Literal["ok", "empty", "error", "unavailable"]
    detail: str | None = None
