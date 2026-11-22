r"""The LangGraph workflow: five agents in a chain, with one safety loop.

START -> 1 request_analyst --(no region: ask the user)--> END
                |
                v
         2 catalog_search  <-------------------------------+
                |                                          | all candidates vetoed
                v                                          | (once, with a wider search)
         3 enrichment  ------------------------------------+
                |  at least one safe trail       \ still none
                v                                 v
         4 route_planner -------------------> 5 presenter -> END
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime

from terramind.graph import routing
from terramind.graph.nodes.catalog_search import catalog_search
from terramind.graph.nodes.common import emit
from terramind.graph.nodes.enrichment import enrichment
from terramind.graph.nodes.presenter import presenter
from terramind.graph.nodes.request_analyst import request_analyst
from terramind.graph.nodes.route_planner import route_planner
from terramind.graph.state import TerraMindContext, TerraMindState

log = logging.getLogger("terramind.timing")
Node = Callable[[TerraMindState, Runtime[TerraMindContext]], Awaitable[dict[str, Any]]]


def timed[F: Node](name: str, fn: F) -> F:
    """Wrap an agent to log and stream how long it took (event "timing", also visible in LangSmith)."""

    @functools.wraps(fn)
    async def wrapper(state: TerraMindState, runtime: Runtime[TerraMindContext]) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            return await fn(state, runtime)
        finally:
            seconds = round(time.perf_counter() - started, 2)
            log.info("agent %s took %.2fs", name, seconds)
            emit(runtime, "timing", node=name, seconds=seconds)

    return cast(F, wrapper)


def build_graph() -> StateGraph[TerraMindState, TerraMindContext]:
    g = StateGraph(TerraMindState, context_schema=TerraMindContext)
    g.add_node("request_analyst", timed("request_analyst", request_analyst))  # agent 1
    g.add_node("catalog_search", timed("catalog_search", catalog_search))  # agent 2
    g.add_node("enrichment", timed("enrichment", enrichment))  # agent 3
    g.add_node("route_planner", timed("route_planner", route_planner))  # agent 4
    g.add_node("presenter", timed("presenter", presenter))  # agent 5

    g.add_edge(START, "request_analyst")
    g.add_conditional_edges("request_analyst", routing.after_request_analyst)
    g.add_edge("catalog_search", "enrichment")
    g.add_conditional_edges("enrichment", routing.after_enrichment)
    g.add_edge("route_planner", "presenter")
    g.add_edge("presenter", END)
    return g


def compile_graph() -> CompiledStateGraph[TerraMindState, TerraMindContext, TerraMindState, TerraMindState]:
    return build_graph().compile(name="terramind")
