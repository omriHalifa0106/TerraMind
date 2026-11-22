"""LangGraph state: a TypedDict of frozen Pydantic models plus resettable reducers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, NotRequired, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from terramind.config import Settings, get_settings
from terramind.llm.client import LLMClient
from terramind.mcp_client.client import ToolGateway
from terramind.schemas import (
    EvidenceChunk,
    FinalAnswer,
    NodeError,
    RoutePlan,
    SafetyVerdict,
    TrailAlert,
    TrailCandidate,
    TrailStatus,
    TripRequest,
    UserProfile,
    WeatherSnapshot,
)

RESET: list[Any] = []  # sentinel object: identity-checked by the reducers below


def union_or_reset(left: list[str] | None, right: list[str] | None) -> list[str]:
    """Ordered set-union; returning RESET from a node clears the channel for a new plan."""
    if right is RESET:
        return []
    return list(dict.fromkeys([*(left or []), *(right or [])]))


def append_or_reset[T](left: list[T] | None, right: list[T] | None) -> list[T]:
    if right is RESET:
        return []
    return [*(left or []), *(right or [])]


Intent = Literal["new_plan", "refine_plan", "question", "smalltalk"]


class TerraMindState(TypedDict):
    # --- conversation (persisted per thread by the checkpointer) ---
    messages: Annotated[list[AnyMessage], add_messages]
    user_id: str

    # --- routing (per turn) ---
    intent: NotRequired[Intent]
    turn_classified: NotRequired[bool]
    intake_done: NotRequired[bool]

    # --- request & memory ---
    request: NotRequired[TripRequest]
    profile: NotRequired[UserProfile]  # loaded from the Store (cross-thread), read-only in the run
    named_trail_ids: NotRequired[list[str]]  # trails the user named explicitly ("נחל כזיב")
    assumptions: NotRequired[list[str]]  # defaults the system filled in (shown to the user)

    # --- hybrid RAG ---
    candidates: NotRequired[list[TrailCandidate]]
    retrieved: NotRequired[list[EvidenceChunk]]  # before grading
    evidence: NotRequired[list[EvidenceChunk]]  # only chunks that passed the grader
    research_rounds: NotRequired[int]
    rewritten_query: NotRequired[str | None]

    # --- planning & safety loop ---
    draft_plan: NotRequired[RoutePlan | None]
    plan_rationale: NotRequired[str | None]  # the planner's reason for its choice (explainer input)
    weather: NotRequired[WeatherSnapshot | None]
    trail_status: NotRequired[TrailStatus | None]
    alerts: NotRequired[list[TrailAlert]]
    verdict: NotRequired[SafetyVerdict]
    verdict_history: Annotated[list[SafetyVerdict], append_or_reset]  # explainer input for trade-offs
    rejected_trail_ids: Annotated[list[str], union_or_reset]
    attempts: NotRequired[int]
    start_shift_minutes: NotRequired[int]

    # --- output & diagnostics ---
    final: NotRequired[FinalAnswer | None]
    errors: Annotated[list[NodeError], append_or_reset]


@dataclass
class TerraMindContext:
    """Run-scoped dependencies, injected through LangGraph's Runtime (swap for fakes in tests)."""

    llm: LLMClient
    tools: ToolGateway
    settings: Settings = field(default_factory=get_settings)


def per_turn_reset() -> dict[str, Any]:
    """Fields cleared at the start of every user turn (memory and plan history stay)."""
    return {
        "turn_classified": False,
        "named_trail_ids": [],
        "intake_done": False,
        "research_rounds": 0,
        "rewritten_query": None,
        "attempts": 0,
        "start_shift_minutes": 0,
        "verdict_history": RESET,
        "rejected_trail_ids": RESET,
        "errors": RESET,
    }


def turn_input(messages: list[AnyMessage], user_id: str) -> TerraMindState:
    """A typed graph input for one user turn (accumulating channels start empty for this write)."""
    return {
        "messages": messages,
        "user_id": user_id,
        "verdict_history": [],
        "rejected_trail_ids": [],
        "errors": [],
    }
