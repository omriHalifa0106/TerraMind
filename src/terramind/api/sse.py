"""Graph stream -> typed SSE events for the browser."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import StreamMode

from terramind.graph.state import turn_input
from terramind.mcp_client.client import start_call_log
from terramind.runtime import AppRuntime

log = logging.getLogger("terramind.api.sse")


def sse(event: str, data: Any) -> dict[str, str]:
    return {"event": event, "data": json.dumps(data, ensure_ascii=False, default=str)}


async def stream_turn(
    rt: AppRuntime, message: str, thread_id: str, user_id: str
) -> AsyncIterator[dict[str, str]]:
    config: RunnableConfig = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": rt.context.settings.recursion_limit,
        "run_name": "terramind-turn",
        "tags": ["api", rt.context.settings.environment],
        "metadata": {"user_id": user_id, "thread_id": thread_id},
    }
    calls = start_call_log()
    modes: list[StreamMode] = ["updates", "custom"]
    yield sse("start", {"thread_id": thread_id})
    try:
        async for mode, chunk in rt.graph.astream(
            turn_input([HumanMessage(content=message)], user_id),
            config,
            context=rt.context,
            stream_mode=modes,
        ):
            if mode == "custom" and isinstance(chunk, dict):
                yield sse(str(chunk.get("event", "custom")), chunk)
            elif mode == "updates" and isinstance(chunk, dict):
                for node, update in chunk.items():
                    if isinstance(update, dict) and node == "ask_clarification":
                        msgs = update.get("messages") or []
                        yield sse("message", {"text": msgs[-1].content if msgs else ""})
                    yield sse("node_done", {"node": node})
    except Exception as exc:  # report, never leave the client hanging
        log.exception("turn failed")
        yield sse("error", {"detail": f"{type(exc).__name__}: {exc}"[:500]})
    yield sse("done", {"tool_calls": calls})
