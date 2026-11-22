"""One graph run -> a stream of typed Server-Sent Events for the browser.

The agents report progress with emit() (LangGraph stream_mode="custom"); each report is forwarded
as one SSE event: start, node, request, tool_calls, candidates, assessment, plan, token, final,
message, timing, error, done.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig

from terramind.graph.state import turn_input
from terramind.mcp_client.client import start_call_log
from terramind.runtime import AppRuntime

log = logging.getLogger("terramind.api.sse")


def sse(event: str, data: Any) -> dict[str, str]:
    return {"event": event, "data": json.dumps(data, ensure_ascii=False, default=str)}


async def stream_turn(rt: AppRuntime, message: str) -> AsyncIterator[dict[str, str]]:
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    config: RunnableConfig = {
        "recursion_limit": rt.context.settings.recursion_limit,
        "run_name": "terramind",  # the run's name in LangSmith
        "tags": ["api", rt.context.settings.environment],
        "metadata": {"run_id": run_id},
    }
    calls = start_call_log()
    yield sse("start", {"run_id": run_id})
    try:
        async for mode, chunk in rt.graph.astream(
            turn_input([HumanMessage(content=message)]),
            config,
            context=rt.context,
            stream_mode=["updates", "custom"],
        ):
            if mode == "custom" and isinstance(chunk, dict):
                yield sse(str(chunk.get("event", "custom")), chunk)
            elif mode == "updates" and isinstance(chunk, dict):
                update = chunk.get("request_analyst")
                if isinstance(update, dict) and update.get("clarification"):
                    yield sse("message", {"text": update["clarification"]})
    except Exception as exc:  # report, never leave the client hanging
        log.exception("run failed")
        yield sse("error", {"detail": f"{type(exc).__name__}: {exc}"[:500]})
    yield sse("done", {"tool_calls": calls})
