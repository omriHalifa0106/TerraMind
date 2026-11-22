"""The long-lived pieces the API needs: the compiled graph, the LLM client and the MCP tool gateway."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

import asyncpg
from langgraph.graph.state import CompiledStateGraph

from terramind.config import Settings, get_settings
from terramind.conversations import ConversationStore
from terramind.graph.builder import compile_graph
from terramind.graph.state import TerraMindContext, TerraMindState
from terramind.llm.client import ClaudeLLM
from terramind.mcp_client.client import McpGateway
from terramind.observability import configure_langsmith

log = logging.getLogger("terramind.runtime")


@dataclass
class AppRuntime:
    graph: CompiledStateGraph[TerraMindState, TerraMindContext, TerraMindState, TerraMindState]
    context: TerraMindContext
    gateway: McpGateway
    conversations: ConversationStore | None = None  # chat memory; None = every message is a new trip


@asynccontextmanager
async def open_runtime(
    settings: Settings | None = None, gateway: McpGateway | None = None
) -> AsyncIterator[AppRuntime]:
    s = settings or get_settings()
    configure_langsmith(s)
    if not (s.anthropic_auth_token or s.anthropic_api_key):
        log.warning("no ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY: every LLM call will fail")
    async with AsyncExitStack() as stack:
        gw = await stack.enter_async_context(gateway or McpGateway.from_settings(s))
        store: ConversationStore | None
        try:
            opened = await ConversationStore.open(s.database_url)
            stack.push_async_callback(opened.close)
            store = opened
        except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
            log.warning(
                "conversation memory unavailable (%s): every message is a new trip", type(exc).__name__
            )
            store = None
        yield AppRuntime(
            graph=compile_graph(),
            context=TerraMindContext(llm=ClaudeLLM(), tools=gw, settings=s),
            gateway=gw,
            conversations=store,
        )
