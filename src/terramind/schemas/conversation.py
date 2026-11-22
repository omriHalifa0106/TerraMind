"""What a chat remembers between messages (stored per thread by the API, given to the graph as input)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from terramind.schemas.base import Frozen
from terramind.schemas.plan import RoutePlan
from terramind.schemas.retrieval import EvidenceChunk, TrailCandidate
from terramind.schemas.safety import CandidateAssessment
from terramind.schemas.trip import TripRequest

HISTORY_LINES = 8  # recent messages kept for agent 1 (user + assistant)


class ChatLine(Frozen):
    role: Literal["user", "assistant"]
    text: str = Field(max_length=4000)


class PreviousTurn(Frozen):
    """The previous turn's results. A follow-up that only changes the day (hours, places, start)
    re-plans from these, without searching again or re-checking safety."""

    request: TripRequest
    candidates: list[TrailCandidate] = Field(default_factory=list)
    assessments: list[CandidateAssessment] = Field(default_factory=list)
    evidence: list[EvidenceChunk] = Field(default_factory=list)
    plan: RoutePlan | None = None
    plan_rationale: str | None = None
    excluded_trail_ids: list[str] = Field(default_factory=list)  # trails the user asked to replace
    history: list[ChatLine] = Field(default_factory=list)
