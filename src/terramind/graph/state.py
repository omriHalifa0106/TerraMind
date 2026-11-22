"""The LangGraph state: what the five agents pass along the chain.

Each agent reads what earlier agents wrote and adds its own part:
  1 request_analyst  -> request, clarification, named_trail_ids, assumptions
  2 catalog_search   -> candidates, evidence, search_rounds
  3 enrichment       -> assessments (one per checked candidate: approved or vetoed, and why)
  4 route_planner    -> plan, plan_rationale
  5 presenter        -> final, messages
One graph run handles one user message; nothing is kept between runs.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass, field
from typing import Annotated, Literal, NotRequired, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from terramind.config import Settings, get_settings
from terramind.llm.client import LLMClient
from terramind.mcp_client.client import ToolGateway
from terramind.schemas import (
    CandidateAssessment,
    EvidenceChunk,
    FinalAnswer,
    NodeError,
    PreviousTurn,
    RoutePlan,
    TrailCandidate,
    TripRequest,
)


class Origin(TypedDict):
    """Where the group drives from: the browser's location or an address the user looked up."""

    lat: float
    lon: float
    label: str


class TerraMindState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]  # the user's message (+ the final answer)
    user_origin: NotRequired[Origin | None]  # "starting from", picked in the UI (not parsed from text)
    user_day_hours: NotRequired[float | None]  # day length picked in the UI (the text wins if it says)
    previous: NotRequired[PreviousTurn | None]  # the chat's previous turn (conversation memory)

    # --- agent 1: request analysis ---
    request: NotRequired[TripRequest]
    clarification: NotRequired[str | None]  # set when the request cannot be planned (no region)
    named_trail_ids: NotRequired[list[str]]  # trails the user named explicitly ("נחל כזיב")
    assumptions: NotRequired[list[str]]  # defaults the system filled in (shown to the user)
    # "replan": a follow-up that only changes the day (hours, places, start, origin): agent 4 re-plans
    # the previous turn's safe trails, agents 2-3 are skipped. "search": search (and check) again.
    scope: NotRequired[Literal["search", "replan"]]
    excluded_trail_ids: NotRequired[list[str]]  # trails the user asked to replace ("a different trail")

    # --- agent 2: catalog search ---
    candidates: NotRequired[list[TrailCandidate]]
    evidence: NotRequired[list[EvidenceChunk]]
    search_rounds: NotRequired[int]

    # --- agent 3: enrichment (accumulates across search rounds, so vetoes can be explained) ---
    assessments: Annotated[list[CandidateAssessment], operator.add]

    # --- agent 4: route planning ---
    plan: NotRequired[RoutePlan | None]
    plan_rationale: NotRequired[str | None]

    # --- agent 5: presentation ---
    final: NotRequired[FinalAnswer]

    errors: Annotated[list[NodeError], operator.add]


@dataclass
class TerraMindContext:
    """Run-scoped dependencies, injected through LangGraph's Runtime (swapped for fakes in tests)."""

    llm: LLMClient
    tools: ToolGateway
    settings: Settings = field(default_factory=get_settings)


def rejected_ids(state: TerraMindState) -> list[str]:
    """Trails agent 3 vetoed so far (in any round)."""
    return list(dict.fromkeys(a.trail_id for a in state.get("assessments", []) if not a.approved))


def approved(state: TerraMindState) -> list[CandidateAssessment]:
    """Approved assessments of the current round, in agent 2's ranking order."""
    current = {c.trail_id for c in state.get("candidates", [])}
    by_id = {a.trail_id: a for a in state.get("assessments", []) if a.approved and a.trail_id in current}
    return [by_id[c.trail_id] for c in state.get("candidates", []) if c.trail_id in by_id]


def turn_input(
    messages: list[AnyMessage],
    origin: Origin | None = None,
    day_hours: float | None = None,
    previous: PreviousTurn | None = None,
) -> TerraMindState:
    """A typed graph input for one user message (accumulating channels start empty)."""
    return {
        "messages": messages,
        "user_origin": origin,
        "user_day_hours": day_hours,
        "previous": previous,
        "assessments": [],
        "errors": [],
    }
