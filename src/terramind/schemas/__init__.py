"""Domain models. Must not import LangGraph (kept framework-agnostic and reusable by MCP servers)."""

from terramind.schemas.base import Frozen, LatLon
from terramind.schemas.conversation import ChatLine, PreviousTurn
from terramind.schemas.plan import FinalAnswer, NodeError, RoutePlan, Stop, StopKind, TradeOff
from terramind.schemas.retrieval import (
    EvidenceChunk,
    LegEstimate,
    NearbyPlace,
    PointOfInterest,
    ToolStatus,
    TrailCandidate,
)
from terramind.schemas.safety import (
    TIMING_CODES,
    AlertKind,
    CandidateAssessment,
    IssueCode,
    SafetyIssue,
    Severity,
    TrailAlert,
    TrailStatus,
    WeatherSnapshot,
    WeatherStatus,
)
from terramind.schemas.trip import Difficulty, GroupProfile, TrailType, TripRequest

__all__ = [
    "TIMING_CODES",
    "AlertKind",
    "CandidateAssessment",
    "ChatLine",
    "Difficulty",
    "EvidenceChunk",
    "FinalAnswer",
    "Frozen",
    "GroupProfile",
    "IssueCode",
    "LatLon",
    "LegEstimate",
    "NearbyPlace",
    "NodeError",
    "PointOfInterest",
    "PreviousTurn",
    "RoutePlan",
    "SafetyIssue",
    "Severity",
    "Stop",
    "StopKind",
    "ToolStatus",
    "TradeOff",
    "TrailAlert",
    "TrailCandidate",
    "TrailStatus",
    "TrailType",
    "TripRequest",
    "WeatherSnapshot",
    "WeatherStatus",
]
