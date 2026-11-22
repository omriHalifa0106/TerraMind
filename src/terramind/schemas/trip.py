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


# Places a day plan can include besides the trail (gis.find_places_near categories).
PlaceKind = Literal[
    "restaurant",
    "cafe",
    "coffee_cart",
    "attraction",
    "museum",
    "winery",
    "brewery",
    "farm",
    "viewpoint",
    "ruins",
    "waterfall",
]


class TrailType(StrEnum):
    WATER = "water"
    VIEWPOINT = "viewpoint"
    FOREST = "forest"
    HISTORY = "history"
    LOOP = "loop"
    FAMILY = "family"


class GroupProfile(Frozen):
    adults: int = Field(default=2, ge=1, le=50, description="Adults in the group")
    children: int = Field(default=0, ge=0, le=50, description="Children in the group ('עם שני ילדים' -> 2)")
    min_child_age: int | None = Field(
        default=None, ge=0, le=17, description="Age of the youngest child, if stated"
    )
    fitness: Literal["low", "medium", "high"] = "medium"
    pace_kmh: float = Field(default=3.5, gt=0.5, lt=8.0)  # digital-twin hook


class TripRequest(Frozen):
    """Structured parameters extracted from free text by the intake node."""

    region: str | None = Field(default=None, description="e.g. 'Upper Galilee', 'Golan', 'Carmel'")
    trip_date: date | None = None
    start_time: time | None = None
    max_duration_hours: float | None = Field(
        default=None, gt=0, le=14, description="Longest WALK the user wants ('מסלול של שעתיים' -> 2)"
    )
    day_hours: float | None = Field(
        default=None,
        gt=0,
        le=14,
        description="Length of the whole day out, hike + places, not counting the drive from home "
        "('יום של 6 שעות' -> 6, 'חצי יום' -> 4, 'יום שלם' -> 9)",
    )
    want_places: list[PlaceKind] = Field(
        default_factory=list,
        description="Places the user asked for besides the hike ('עם מסעדה' -> restaurant, "
        "'עגלת קפה' -> coffee_cart, 'יקב' -> winery, 'מוזיאון' -> museum)",
    )
    kosher_only: bool = Field(default=False, description="Only kosher food places ('כשר')")
    difficulty: Difficulty | None = None
    trail_types: list[TrailType] = Field(default_factory=list)
    max_length_km: float | None = Field(default=None, gt=0, le=40)
    max_drive_minutes: int | None = Field(default=None, gt=0, le=300)
    origin: LatLon | None = Field(default=None, description="Where the group drives from")
    origin_label: str | None = Field(default=None, description="Human name of the origin")
    group: GroupProfile = Field(default_factory=GroupProfile)
    must_have: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    semantic_query: str = Field(description="One-line normalized intent used for vector search")
    missing_fields: list[Literal["region", "trip_date"]] = Field(default_factory=list)
