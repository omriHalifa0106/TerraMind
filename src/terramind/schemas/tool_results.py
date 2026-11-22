"""Typed results of MCP tools. Shared by the servers (producers) and the agents (consumers)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from terramind.schemas.base import Frozen
from terramind.schemas.retrieval import EvidenceChunk, NearbyPlace, PointOfInterest, TrailCandidate
from terramind.schemas.safety import TrailAlert, TrailStatus

Status = Literal["ok", "empty", "error", "unavailable"]


class RegionInfo(Frozen):
    key: str
    name_en: str
    name_he: str
    bbox: tuple[float, float, float, float]


class RegionList(Frozen):
    status: Status = "ok"
    regions: list[RegionInfo] = Field(default_factory=list)


class TrailSearchResult(Frozen):
    status: Status
    detail: str | None = None
    trails: list[TrailCandidate] = Field(default_factory=list)


class TrailDetails(Frozen):
    status: Status
    detail: str | None = None
    trail: TrailCandidate | None = None
    osmc_symbol: str | None = None
    colour: str | None = None
    trail_end: tuple[float, float] | None = None


class PoiResult(Frozen):
    status: Status
    detail: str | None = None
    pois: list[PointOfInterest] = Field(default_factory=list)


class GeoJsonResult(Frozen):
    status: Status
    detail: str | None = None
    feature: dict[str, Any] | None = None


class EvidenceResult(Frozen):
    status: Status
    detail: str | None = None
    chunks: list[EvidenceChunk] = Field(default_factory=list)


class AlertsResult(Frozen):
    status: Status
    detail: str | None = None
    alerts: list[TrailAlert] = Field(default_factory=list)


class TrailStatusResult(Frozen):
    status: Status
    detail: str | None = None
    trail: TrailStatus | None = None


class Place(Frozen):
    label: str
    lat: float
    lon: float


class GeocodeResult(Frozen):
    """Address / place name -> coordinates (the user's starting point)."""

    status: Status
    detail: str | None = None
    places: list[Place] = Field(default_factory=list)


class NearbyPlacesResult(Frozen):
    status: Status
    detail: str | None = None
    places: list[NearbyPlace] = Field(default_factory=list)
