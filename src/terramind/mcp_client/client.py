"""Tool gateway: the agents' only door to data. Connects to the four MCP servers with fastmcp.Client.

We do not use langchain-mcp-adapters here: its current release requires `mcp<2`, while
FastMCP 4 requires `mcp>=2`. This thin gateway gives us the same thing with one dependency:
typed calls, per-call timeouts, failures returned as data, a per-run call log, and
LangChain tools generated from the MCP schemas for native tool calling.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import time
from contextlib import AsyncExitStack
from typing import Any, Literal, Protocol

from fastmcp import Client, FastMCP
from langchain_core.tools import BaseTool, StructuredTool

from terramind.config import Settings, get_settings

log = logging.getLogger("terramind.tools")

Server = Literal["gis", "knowledge", "weather", "alerts"]
RECONNECT_COOLDOWN_S = 10.0

# Per-run call log (a ContextVar, so concurrent requests do not mix their logs).
_call_log: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "tool_calls", default=None
)


def start_call_log() -> list[dict[str, Any]]:
    log_list: list[dict[str, Any]] = []
    _call_log.set(log_list)
    return log_list


class ToolGateway(Protocol):
    async def call(self, server: Server, tool: str, args: dict[str, Any]) -> dict[str, Any]: ...

    def langchain_tools(self, server: Server, names: list[str]) -> list[BaseTool]: ...


class McpGateway:
    """Holds one open MCP session per server. Use as an async context manager (app lifespan)."""

    def __init__(self, targets: dict[Server, str | FastMCP], timeout_s: float) -> None:
        self._targets = targets
        self._timeout_s = timeout_s
        self._clients: dict[Server, Client[Any]] = {}
        self._schemas: dict[tuple[Server, str], tuple[str, dict[str, Any]]] = {}
        self._stack = AsyncExitStack()
        self._lock = asyncio.Lock()
        self._last_attempt: dict[Server, float] = {}

    @classmethod
    def from_settings(cls, s: Settings | None = None) -> McpGateway:
        s = s or get_settings()
        return cls(
            {
                "gis": s.mcp_gis_url,
                "knowledge": s.mcp_knowledge_url,
                "weather": s.mcp_weather_url,
                "alerts": s.mcp_alerts_url,
            },
            timeout_s=s.mcp_tool_timeout_s,
        )

    async def __aenter__(self) -> McpGateway:
        for server in self._targets:
            await self._connect(server)
        return self

    async def _connect(self, server: Server) -> Client[Any] | None:
        """Open a session to one server. A server that is down (or still loading, like the
        knowledge server downloading its model) is retried on later calls, at most every
        RECONNECT_COOLDOWN_S, instead of being lost for the lifetime of the app."""
        async with self._lock:
            if (existing := self._clients.get(server)) is not None:
                return existing
            now = time.monotonic()
            if now - self._last_attempt.get(server, -RECONNECT_COOLDOWN_S) < RECONNECT_COOLDOWN_S:
                return None
            self._last_attempt[server] = now
            client: Client[Any] = Client(self._targets[server], timeout=self._timeout_s)
            try:
                await self._stack.enter_async_context(client)
                tools = await client.list_tools()
            except Exception as exc:  # a missing server degrades the system, it does not crash it
                log.warning("MCP server %s unreachable (%s); will retry on demand", server, exc)
                return None
            self._clients[server] = client
            for tool in tools:
                self._schemas[(server, tool.name)] = (tool.description or "", tool.input_schema)
            log.info("MCP server %s connected (%d tools)", server, len(tools))
            return client

    async def ensure_connected(self) -> list[Server]:
        """Try every missing server once (respecting the cooldown); returns the connected ones."""
        for server in self._targets:
            if server not in self._clients:
                await self._connect(server)
        return self.connected

    async def __aexit__(self, *exc: object) -> None:
        await self._stack.aclose()

    @property
    def connected(self) -> list[Server]:
        return list(self._clients)

    async def call(self, server: Server, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        clean = {k: v for k, v in args.items() if v is not None}
        result: dict[str, Any]
        client = self._clients.get(server) or await self._connect(server)
        if client is None:
            result = {"status": "unavailable", "detail": f"MCP server {server!r} is not connected"}
        else:
            try:
                res = await asyncio.wait_for(
                    client.call_tool(tool, clean, raise_on_error=False), timeout=self._timeout_s
                )
                if res.is_error:
                    text = " ".join(getattr(c, "text", "") for c in res.content)[:300]
                    result = {"status": "error", "detail": text or "tool error"}
                else:
                    result = dict(res.structured_content or {})
            except TimeoutError:
                result = {"status": "unavailable", "detail": f"{server}.{tool} timed out"}
            except Exception as exc:
                log.warning("tool %s.%s failed: %s", server, tool, exc)
                result = {"status": "error", "detail": f"{type(exc).__name__}: {exc}"[:300]}
        entry = {
            "server": server,
            "tool": tool,
            "args": clean,
            "status": result.get("status", "ok"),
            "ms": round((time.perf_counter() - started) * 1000),
        }
        if (call_log := _call_log.get()) is not None:
            call_log.append(entry)
        return result

    def langchain_tools(self, server: Server, names: list[str]) -> list[BaseTool]:
        """Expose selected MCP tools to an LLM for native tool calling (least privilege per node)."""
        tools: list[BaseTool] = []
        for name in names:
            if (server, name) not in self._schemas:
                continue
            description, schema = self._schemas[(server, name)]

            async def _run(_server: Server = server, _name: str = name, **kwargs: Any) -> str:
                return json.dumps(await self.call(_server, _name, kwargs), ensure_ascii=False, default=str)

            tools.append(
                StructuredTool.from_function(
                    coroutine=_run, name=name, description=description[:1000] or name, args_schema=schema
                )
            )
        return tools
