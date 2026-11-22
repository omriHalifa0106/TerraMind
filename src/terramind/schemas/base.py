from __future__ import annotations

from pydantic import BaseModel, ConfigDict

LatLon = tuple[float, float]  # (lat, lon), WGS84


class Frozen(BaseModel):
    """Base for state payloads: immutable, strict about unknown fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")
