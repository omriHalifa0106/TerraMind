"""Builds the long-lived runtime pieces (LLM client, tool gateway, checkpointer, store)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.memory import InMemoryStore
from langgraph.store.postgres.aio import AsyncPostgresStore

from terramind.config import Settings, get_settings
from terramind.db import connect
from terramind.graph.builder import SERDE, compile_graph
from terramind.graph.state import TerraMindContext, TerraMindState
from terramind.llm.client import ClaudeLLM, LLMClient
from terramind.llm.heuristic import HeuristicLLM
from terramind.mcp_client.client import McpGateway

log = logging.getLogger("terramind.runtime")


def build_llm(s: Settings) -> LLMClient:
    if s.llm_backend == "heuristic":
        return HeuristicLLM()
    provider = s.resolved_llm_provider
    if not s.llm_key:
        log.warning("no API key for provider %r: falling back to the offline HeuristicLLM", provider)
        return HeuristicLLM()
    log.info(
        "LLM provider: %s%s",
        provider,
        f" via {s.anthropic_base_url}" if provider == "anthropic" and s.anthropic_base_url else "",
    )
    if provider == "openrouter" and not s.openrouter_api_key.startswith("sk-or-"):
        log.warning(
            "OPENROUTER_API_KEY does not look like an OpenRouter key (expected sk-or-v1-...); "
            "calls will likely fail with 401 and nodes will use their offline fallbacks"
        )
    return ClaudeLLM()


@dataclass
class AppRuntime:
    graph: CompiledStateGraph[TerraMindState, TerraMindContext, TerraMindState, TerraMindState]
    context: TerraMindContext
    gateway: McpGateway
    persistent: bool


@asynccontextmanager
async def open_runtime(
    settings: Settings | None = None, gateway: McpGateway | None = None, persistent: bool = True
) -> AsyncIterator[AppRuntime]:
    """Postgres checkpointer + store when `persistent`, in-memory otherwise (tests, CLI demo)."""
    s = settings or get_settings()
    async with AsyncExitStack() as stack:
        gw = await stack.enter_async_context(gateway or McpGateway.from_settings(s))
        if persistent:
            # Fail fast with an actionable message instead of psycopg's connection-timeout traceback.
            await (await connect(s.database_url, connect_timeout=5)).close()
            saver = await stack.enter_async_context(
                AsyncPostgresSaver.from_conn_string(s.database_url, serde=SERDE)
            )
            store = await stack.enter_async_context(AsyncPostgresStore.from_conn_string(s.database_url))
            await saver.setup()
            await store.setup()
            graph = compile_graph(saver, store)
        else:
            graph = compile_graph(InMemorySaver(serde=SERDE), InMemoryStore())
        yield AppRuntime(
            graph=graph,
            context=TerraMindContext(llm=build_llm(s), tools=gw, settings=s),
            gateway=gw,
            persistent=persistent,
        )
