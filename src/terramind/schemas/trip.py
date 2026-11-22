from __future__ import annotations

from datetime import date, time
from enum import StrEnum
from typing import Literal

from pydantic import Field

from terramind.schemas.base import Frozen, LatLon


class Difficulty(StrEnum):
    EASY = "easy"
    MODERATE = "moderate"
    HARD = "hard"


class TrailType(StrEnum):
    WATER = "water"
    VIEWPOINT = "viewpoint"
    FOREST = "forest"
    HISTORY = "history"
    LOOP = "loop"
    FAMILY = "family"


class GroupProfile(Frozen):
    adults: int = Field(default=2, ge=1, le=50)
    children: int = Field(default=0, ge=0, le=50)
    min_child_age: int | None = Field(default=None, ge=0, le=17)
    fitness: Literal["low", "medium", "high"] = "medium"
    pace_kmh: float = Field(default=3.5, gt=0.5, lt=8.0)  # digital-twin hook


class TripRequest(Frozen):
    """Structured parameters extracted from free text by the intake node."""

    region: str | None = Field(default=None, description="e.g. 'Upper Galilee', 'Golan', 'Carmel'")
    trip_date: date | None = None
    start_time: time | None = None
    max_duration_hours: float | None = Field(default=None, gt=0, le=14)
    difficulty: Difficulty | None = None
    trail_types: list[TrailType] = Field(default_factory=list)
    max_length_km: float | None = Field(default=None, gt=0, le=40)
    max_drive_minutes: int | None = Field(default=None, gt=0, le=300)
    origin: LatLon | None = Field(default=None, description="Where the group drives from")
    group: GroupProfile = Field(default_factory=GroupProfile)
    must_have: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    semantic_query: str = Field(description="One-line normalized intent used for vector search")
    missing_fields: list[Literal["region", "trip_date"]] = Field(default_factory=list)
