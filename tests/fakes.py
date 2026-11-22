"""A scripted stand-in for Claude, so graph tests run without network or API costs.

Each test says what agent 1 should "understand" (a TripRequest); the fake then behaves like a
simple but valid model: agent 2 gets no tool calls (so the code's own search arguments are used),
agent 4 picks the first candidate it is offered, agent 5 writes a fixed sentence.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Sequence
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from terramind.graph.nodes.route_planner import PlanDraft
from terramind.llm.client import LLMError
from terramind.llm.router import Task
from terramind.schemas import TripRequest

CANDIDATES = re.compile(r"CANDIDATES: (\[.*?\])\nNAMED BY USER", re.S)


class FakeLLM:
    def __init__(self, request: TripRequest, tool_calls: list[dict[str, Any]] | None = None) -> None:
        self.request = request
        self.scripted_tool_calls = tool_calls or []
        self.prompts: dict[str, str] = {}  # task -> last user-side prompt, for assertions

    async def structured[T: BaseModel](
        self,
        task: Task,
        schema: type[T],
        messages: Sequence[BaseMessage],
        config: RunnableConfig | None = None,
    ) -> T:
        self.prompts[task] = str(messages[-1].content)
        if schema is TripRequest:
            return self.request  # type: ignore[return-value]
        if schema is PlanDraft:
            match = CANDIDATES.search(self.prompts[task])
            first = json.loads(match.group(1))[0] if match else {}
            draft = PlanDraft(trail_id=first.get("trail_id", "?"), title="Plan", rationale="Best match.")
            return draft  # type: ignore[return-value]
        raise AssertionError(f"unexpected structured call: {task} {schema}")

    async def tool_calls(
        self,
        task: Task,
        messages: Sequence[BaseMessage],
        tools: Sequence[BaseTool],
        config: RunnableConfig | None = None,
    ) -> list[dict[str, Any]]:
        self.prompts[task] = str(messages[-1].content)
        self.tools_offered = sorted(t.name for t in tools)
        return self.scripted_tool_calls

    async def stream_text(
        self, task: Task, messages: Sequence[BaseMessage], config: RunnableConfig | None = None
    ) -> AsyncIterator[str]:
        self.prompts[task] = str(messages[-1].content)
        for token in ("Here is ", "your plan."):
            yield token


class DownLLM(FakeLLM):
    """Every call fails, like a rejected API key."""

    async def structured[T: BaseModel](
        self,
        task: Task,
        schema: type[T],
        messages: Sequence[BaseMessage],
        config: RunnableConfig | None = None,
    ) -> T:
        raise LLMError("401")
