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

from terramind.conversations import next_turn
from terramind.graph.state import Origin, turn_input
from terramind.mcp_client.client import start_call_log
from terramind.runtime import AppRuntime

log = logging.getLogger("terramind.api.sse")


def sse(event: str, data: Any) -> dict[str, str]:
    return {"event": event, "data": json.dumps(data, ensure_ascii=False, default=str)}


async def stream_turn(
    rt: AppRuntime,
    message: str,
    origin: Origin | None = None,
    day_hours: float | None = None,
    thread_id: str | None = None,
) -> AsyncIterator[dict[str, str]]:
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    store = rt.conversations if thread_id else None
    previous = await store.load(thread_id) if store and thread_id else None
    metadata: dict[str, Any] = {"run_id": run_id}
    if thread_id:
        metadata["thread_id"] = thread_id  # LangSmith groups the turns of one chat as a thread
    config: RunnableConfig = {
        "recursion_limit": rt.context.settings.recursion_limit,
        "run_name": "terramind",  # the run's name in LangSmith
        "tags": ["api", rt.context.settings.environment],
        "metadata": metadata,
    }
    calls = start_call_log()
    yield sse("start", {"run_id": run_id, "follow_up": previous is not None})
    final_state: dict[str, Any] = {}
    try:
        async for mode, chunk in rt.graph.astream(
            turn_input([HumanMessage(content=message)], origin, day_hours, previous),
            config,
            context=rt.context,
            stream_mode=["updates", "custom", "values"],
        ):
            if mode == "values" and isinstance(chunk, dict):
                final_state = chunk
            elif mode == "custom" and isinstance(chunk, dict):
                yield sse(str(chunk.get("event", "custom")), chunk)
            elif mode == "updates" and isinstance(chunk, dict):
                update = chunk.get("request_analyst")
                if isinstance(update, dict) and update.get("clarification"):
                    yield sse("message", {"text": update["clarification"]})
    except Exception as exc:  # report, never leave the client hanging
        log.exception("run failed")
        yield sse("error", {"detail": f"{type(exc).__name__}: {exc}"[:500]})
    if store and thread_id and final_state:
        final = final_state.get("final")
        answer = final.summary_md if final else (final_state.get("clarification") or "")
        if (turn := next_turn(final_state, previous, message, answer)) is not None:
            await store.save(thread_id, turn)
    yield sse("done", {"tool_calls": calls})
