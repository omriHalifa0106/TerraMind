"""Conditional edges: which agent runs next. Pure functions of the state, tested without any LLM."""

from __future__ import annotations

from typing import Literal

from terramind.config import get_settings
from terramind.graph.state import TerraMindState, approved


def after_request_analyst(state: TerraMindState) -> Literal["catalog_search", "__end__"]:
    """No region (or the model failed): agent 1 already asked the user, so stop here."""
    return "__end__" if state.get("clarification") else "catalog_search"  # "__end__" is LangGraph's END


def after_enrichment(state: TerraMindState) -> Literal["route_planner", "catalog_search", "presenter"]:
    """At least one safe trail -> plan it. All vetoed -> search again, wider, once. Still nothing ->
    the presenter explains why there is no safe plan (never a guessed one)."""
    if approved(state):
        return "route_planner"
    if state.get("search_rounds", 0) < get_settings().max_search_rounds:
        return "catalog_search"
    return "presenter"
