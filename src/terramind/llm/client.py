"""LLM access behind a small protocol, so agents can run against Claude or a scripted fake in tests.

Three ways an agent uses the model:
- structured(): the answer is a Pydantic object (agent 1's TripRequest, agent 4's PlanDraft).
- tool_calls(): the model chooses which tools to call and with what arguments (agent 2).
- stream_text(): free text, streamed token by token to the browser (agent 5).
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Sequence
from typing import Any, Protocol

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from terramind.llm.router import STRUCTURED_METHODS, StructuredMethod, Task, get_chat_model, model_id_for

log = logging.getLogger("terramind.llm")


class LLMError(RuntimeError):
    """The model call failed after retries, or returned an invalid structure."""


def _is_fatal(exc: BaseException) -> bool:
    """Errors another structured-output method will not fix (auth, credits, unknown model)."""
    text = f"{type(exc).__name__} {exc}"
    return any(k in text for k in ("401", "402", "AuthenticationError", "PermissionDenied", "NotFoundError"))


def describe(exc: BaseException) -> str:
    """Short, actionable text for the most common provider failures."""
    text = str(exc)
    if "401" in text or "AuthenticationError" in type(exc).__name__:
        return (
            "The Anthropic API rejected the key (HTTP 401): check ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL."
        )
    if "404" in text or "not_found" in text:
        return "Unknown model ID (HTTP 404): check ANTHROPIC_MODEL_SMART / ANTHROPIC_MODEL_FAST in .env."
    return f"{type(exc).__name__}: {text}"[:300]


class LLMClient(Protocol):
    async def structured[T: BaseModel](
        self,
        task: Task,
        schema: type[T],
        messages: Sequence[BaseMessage],
        config: RunnableConfig | None = None,
    ) -> T: ...

    async def tool_calls(
        self,
        task: Task,
        messages: Sequence[BaseMessage],
        tools: Sequence[BaseTool],
        config: RunnableConfig | None = None,
    ) -> list[dict[str, Any]]: ...

    def stream_text(
        self, task: Task, messages: Sequence[BaseMessage], config: RunnableConfig | None = None
    ) -> AsyncIterator[str]: ...


class ClaudeLLM:
    """Production client for the Anthropic API."""

    def __init__(self) -> None:
        # model id -> the structured-output method that last worked. Tried first next time, so an
        # unsupported method costs one failed round trip per process, not one per call.
        self._working_method: dict[str, StructuredMethod] = {}

    def _methods_for(self, task: Task) -> list[StructuredMethod]:
        preferred = self._working_method.get(model_id_for(task))
        if preferred is not None:
            return [preferred, *(m for m in STRUCTURED_METHODS if m != preferred)]
        return list(STRUCTURED_METHODS)

    async def structured[T: BaseModel](
        self,
        task: Task,
        schema: type[T],
        messages: Sequence[BaseMessage],
        config: RunnableConfig | None = None,
    ) -> T:
        last_error: Exception | None = None
        for method in self._methods_for(task):
            model = get_chat_model(task).with_structured_output(schema, method=method)
            msgs = list(messages)
            for _attempt in range(2):  # one repair attempt with the validation error fed back
                started = time.perf_counter()
                try:
                    result = await model.ainvoke(msgs, config=config)
                except Exception as exc:  # provider / parsing errors after transport retries
                    last_error = exc
                    if _is_fatal(exc):
                        raise LLMError(f"{task}: {describe(exc)}") from exc
                    if "validation" in str(exc).lower() or "parse" in str(exc).lower():
                        msgs = [*msgs, AIMessage(content=f"Previous output was invalid: {exc}. Fix it.")]
                        continue
                    break  # e.g. method not supported by this model/gateway: try the next method
                log.info("%s: %s call took %.2fs", task, method, time.perf_counter() - started)
                try:
                    validated = result if isinstance(result, schema) else schema.model_validate(result)
                except Exception as exc:
                    last_error = exc
                    msgs = [*msgs, AIMessage(content=f"Previous output was invalid: {exc}. Fix it.")]
                    continue
                self._working_method[model_id_for(task)] = method
                return validated
        raise LLMError(f"{task}: {describe(last_error) if last_error else 'unknown error'}") from last_error

    async def tool_calls(
        self,
        task: Task,
        messages: Sequence[BaseMessage],
        tools: Sequence[BaseTool],
        config: RunnableConfig | None = None,
    ) -> list[dict[str, Any]]:
        """One model turn with tools bound. Returns the tool calls it asked for (not executed here),
        e.g. [{"name": "search_trails", "args": {...}}, {"name": "hybrid_search", "args": {...}}]."""
        model = get_chat_model(task).bind_tools(list(tools))
        try:
            ai = await model.ainvoke(list(messages), config=config)
        except Exception as exc:
            raise LLMError(f"{task}: {describe(exc)}") from exc
        if not isinstance(ai, AIMessage):
            return []
        return [{"name": c["name"], "args": c["args"]} for c in ai.tool_calls]

    async def stream_text(
        self, task: Task, messages: Sequence[BaseMessage], config: RunnableConfig | None = None
    ) -> AsyncIterator[str]:
        model = get_chat_model(task, streaming=True)
        try:
            async for chunk in model.astream(list(messages), config=config):
                # Content may arrive as a list of blocks (text, thinking...): `.text` keeps only text.
                if text := chunk.text:
                    yield text
        except Exception as exc:
            raise LLMError(f"{task} stream: {describe(exc)}") from exc
