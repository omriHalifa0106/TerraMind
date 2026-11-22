"""Edge functions. Pure functions of the state, unit-tested without any LLM."""

from __future__ import annotations

from typing import Literal

from terramind.config import get_settings
from terramind.graph.state import TerraMindState

SupervisorRoute = Literal["intake", "ask_clarification", "researcher", "explainer"]


def route_supervisor(state: TerraMindState) -> SupervisorRoute:
    intent = state.get("intent", "new_plan")
    if intent == "smalltalk":
        return "ask_clarification"
    if intent == "question" and state.get("final") is not None:
        return "explainer"
    if not state.get("intake_done"):
        return "intake"
    request = state.get("request")
    if request is None or "region" in request.missing_fields:
        return "ask_clarification"
    return "researcher"


def route_after_grader(state: TerraMindState) -> Literal["researcher", "planner", "safe_fallback"]:
    s = get_settings()
    if not state.get("candidates"):
        if state.get("research_rounds", 0) < s.max_research_rounds:
            return "researcher"  # second round relaxes the spatial filters
        return "safe_fallback"
    if (
        len(state.get("evidence", [])) < s.min_relevant_chunks
        and state.get("research_rounds", 0) < s.max_research_rounds
        and state.get("rewritten_query")
    ):
        return "researcher"
    return "planner"


def route_after_planner(state: TerraMindState) -> Literal["enrich", "safe_fallback"]:
    return "enrich" if state.get("draft_plan") is not None else "safe_fallback"


def route_after_critic(
    state: TerraMindState,
) -> Literal["explainer", "planner", "researcher", "safe_fallback"]:
    verdict = state["verdict"]
    if verdict.approved:
        return "explainer"
    if state.get("attempts", 0) >= get_settings().max_safety_attempts:
        return "safe_fallback"
    if verdict.replan_scope == "timing":
        return "planner"
    # Trail veto: the planner can pick from the candidates already found (the vetoed ones are
    # filtered out there). Search again only when none is left.
    rejected = set(state.get("rejected_trail_ids", []))
    remaining = [c for c in state.get("candidates", []) if c.trail_id not in rejected]
    return "planner" if remaining else "researcher"
