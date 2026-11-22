from __future__ import annotations

from datetime import date, datetime, time
from enum import StrEnum
from typing import Literal

from pydantic import Field

from terramind.schemas.base import Frozen


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


WeatherStatus = Literal["ok", "cached", "out_of_range", "unavailable"]


class WeatherSnapshot(Frozen):
    trail_id: str
    for_date: date
    temp_max_c: float | None = None
    apparent_temp_max_c: float | None = None
    precip_prob_max_pct: int | None = None
    precip_sum_mm: float | None = None
    wind_gust_max_kmh: float | None = None
    uv_index_max: float | None = None
    sunrise: datetime | None = None
    sunset: datetime | None = None
    hourly_precip_prob: dict[int, int] = Field(default_factory=dict)  # local hour -> %
    status: WeatherStatus = "ok"

    @classmethod
    def unavailable(cls, trail_id: str, for_date: date, status: WeatherStatus) -> WeatherSnapshot:
        return cls(trail_id=trail_id, for_date=for_date, status=status)


AlertKind = Literal["closure", "flood", "fire", "crowding", "military", "maintenance"]


class TrailAlert(Frozen):
    alert_id: str
    trail_id: str | None
    region: str | None
    kind: AlertKind
    severity: Severity
    message: str
    valid_from: datetime
    valid_to: datetime | None


class TrailStatus(Frozen):
    trail_id: str
    is_open: bool
    reason: str | None = None
    crowd_level: Literal["low", "medium", "high", "unknown"] = "unknown"
    status: Literal["ok", "unavailable"] = "ok"


IssueCode = Literal[
    "CLOSED",
    "HEAT",
    "FLOOD_RISK",
    "STORM",
    "INSUFFICIENT_DAYLIGHT",
    "DIFFICULTY_MISMATCH",
    "CROWDED",
    "DATA_UNAVAILABLE",
]
TIMING_CODES: frozenset[str] = frozenset({"INSUFFICIENT_DAYLIGHT", "HEAT"})


class SafetyIssue(Frozen):
    trail_id: str
    trail_name: str | None = None
    code: IssueCode
    severity: Severity
    evidence: str = Field(description="Cites the tool result that triggered the issue")
    source: Literal["rule"] = "rule"  # vetoes come only from safety/rules.py, never from the LLM


class CandidateAssessment(Frozen):
    """Agent 3's verdict on one candidate trail, decided by the deterministic rules."""

    trail_id: str
    trail_name: str | None = None
    approved: bool
    start_time: time  # the start the rules accepted (may be earlier than requested, for daylight/heat)
    issues: list[SafetyIssue] = Field(default_factory=list)
    weather: WeatherSnapshot | None = None
    trail_status: TrailStatus | None = None
    alerts: list[TrailAlert] = Field(default_factory=list)
