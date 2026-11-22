"""Conditional edges: which agent runs next. Pure functions of the state, tested without any LLM."""

from __future__ import annotations

from typing import Literal

from terramind.config import get_settings
from terramind.graph.state import TerraMindState, approved


def after_request_analyst(state: TerraMindState) -> Literal["catalog_search", "route_planner", "__end__"]:
    """No region (or the model failed): agent 1 already asked the user, so stop here ("__end__" is
    LangGraph's END). A follow-up that only changes the day goes straight to agent 4, which re-plans
    the trails agent 3 approved in the previous turn. Anything else searches (and checks) again."""
    if state.get("clarification"):
        return "__end__"
    return "route_planner" if state.get("scope") == "replan" else "catalog_search"


def after_enrichment(state: TerraMindState) -> Literal["route_planner", "catalog_search", "presenter"]:
    """At least one safe trail -> plan it. All vetoed -> search again, wider, once. Still nothing ->
    the presenter explains why there is no safe plan (never a guessed one)."""
    if approved(state):
        return "route_planner"
    if state.get("search_rounds", 0) < get_settings().max_search_rounds:
        return "catalog_search"
    return "presenter"
