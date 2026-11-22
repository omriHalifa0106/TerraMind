from __future__ import annotations

from datetime import time
from typing import Literal

from pydantic import Field

from terramind.schemas.base import Frozen, LatLon
from terramind.schemas.safety import IssueCode

StopKind = Literal["parking", "trailhead", "poi", "viewpoint", "water", "food", "end"]


class Stop(Frozen):
    order: int = Field(ge=0)
    name: str
    kind: StopKind
    location: LatLon
    arrive_at: time | None = None
    dwell_minutes: int = Field(default=0, ge=0)
    note: str | None = None


class RoutePlan(Frozen):
    plan_id: str
    trail_id: str
    title: str
    stops: list[Stop] = Field(min_length=2)
    walk_km: float
    walk_minutes: int
    drive_minutes: int | None = None
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
