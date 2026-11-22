"""Graph wiring and checkpoint serialization."""

from __future__ import annotations

import enum
import functools
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.store.base import BaseStore
from langgraph.types import Checkpointer
from pydantic import BaseModel

from terramind import schemas
from terramind.graph import routing
from terramind.graph.nodes.common import emit
from terramind.graph.nodes.conversation import (
    ask_clarification,
    intake,
    load_profile,
    save_memory,
    supervisor,
)
from terramind.graph.nodes.explainer import explainer
from terramind.graph.nodes.planning import enrich, planner
from terramind.graph.nodes.retrieval import grader, researcher
from terramind.graph.nodes.safety import safe_fallback, safety_critic
from terramind.graph.state import TerraMindContext, TerraMindState


def allowed_msgpack_types() -> list[tuple[str, str]]:
    """Every domain model that may appear in the state, registered for safe deserialization."""
    return sorted(
        {
            (obj.__module__, obj.__name__)
            for obj in vars(schemas).values()
            if isinstance(obj, type)
            and issubclass(obj, BaseModel | enum.Enum)
            and obj.__module__.startswith("terramind.schemas")
        }
    )


SERDE = JsonPlusSerializer(allowed_msgpack_modules=allowed_msgpack_types())
log = logging.getLogger("terramind.timing")


def timed[F: Callable[[TerraMindState, Runtime[TerraMindContext]], Awaitable[dict[str, Any]]]](
    name: str, fn: F
) -> F:
    """Wrap a node to log and stream its wall-clock duration (custom event "timing")."""

    @functools.wraps(fn)
    async def wrapper(state: TerraMindState, runtime: Runtime[TerraMindContext]) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            return await fn(state, runtime)
        finally:
            seconds = round(time.perf_counter() - started, 2)
            log.info("node %s took %.2fs", name, seconds)
            emit(runtime, "timing", node=name, seconds=seconds)

    return cast(F, wrapper)


def build_graph() -> StateGraph[TerraMindState, TerraMindContext]:
    g = StateGraph(TerraMindState, context_schema=TerraMindContext)
    g.add_node("load_profile", timed("load_profile", load_profile))
    g.add_node("supervisor", timed("supervisor", supervisor))
    g.add_node("intake", timed("intake", intake))
    g.add_node("ask_clarification", timed("ask_clarification", ask_clarification))
    g.add_node("researcher", timed("researcher", researcher))
    g.add_node("grader", timed("grader", grader))
    g.add_node("planner", timed("planner", planner))
    g.add_node("enrich", timed("enrich", enrich))
    g.add_node("safety_critic", timed("safety_critic", safety_critic))
    g.add_node("safe_fallback", timed("safe_fallback", safe_fallback))
    g.add_node("explainer", timed("explainer", explainer))
    g.add_node("save_memory", timed("save_memory", save_memory))

    g.add_edge(START, "load_profile")
    g.add_edge("load_profile", "supervisor")
    g.add_conditional_edges("supervisor", routing.route_supervisor)
    g.add_edge("intake", "supervisor")
    g.add_edge("ask_clarification", END)
    g.add_edge("researcher", "grader")
    g.add_conditional_edges("grader", routing.route_after_grader)
    g.add_conditional_edges("planner", routing.route_after_planner)
    g.add_edge("enrich", "safety_critic")
    g.add_conditional_edges("safety_critic", routing.route_after_critic)
    g.add_edge("safe_fallback", "explainer")
    g.add_edge("explainer", "save_memory")
    g.add_edge("save_memory", END)
    return g


def compile_graph(
    checkpointer: Checkpointer = None, store: BaseStore | None = None
) -> CompiledStateGraph[TerraMindState, TerraMindContext, TerraMindState, TerraMindState]:
    return build_graph().compile(checkpointer=checkpointer, store=store, name="terramind")
