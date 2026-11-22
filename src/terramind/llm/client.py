"""LLM access as a small protocol, so graph nodes can run against OpenRouter or a scripted fake."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Sequence
from typing import Any, Protocol

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from terramind.config import get_settings
from terramind.llm.router import Task, get_chat_model, model_id_for, structured_methods

log = logging.getLogger("terramind.llm")


class LLMError(RuntimeError):
    """Raised when the model call fails after retries/fallbacks or returns an invalid structure."""


def _is_fatal(exc: BaseException) -> bool:
    """Errors another structured-output method will not fix (auth, credits, unknown model)."""
    text = f"{type(exc).__name__} {exc}"
    return any(k in text for k in ("401", "402", "AuthenticationError", "PermissionDenied", "NotFoundError"))


def describe(exc: BaseException) -> str:
    """Short, actionable text for the most common provider failures."""
    text = str(exc)
    if "401" in text or "AuthenticationError" in type(exc).__name__:
        return (
            "The LLM provider rejected the API key (HTTP 401). For OpenRouter, OPENROUTER_API_KEY must be an "
            "sk-or-v1- key; for Anthropic or a gateway, check ANTHROPIC_AUTH_TOKEN and ANTHROPIC_BASE_URL."
        )
    if "402" in text:
        return "OpenRouter: insufficient credits (HTTP 402). Add credits at https://openrouter.ai/credits."
    if "404" in text or "not a valid model" in text.lower() or "not_found" in text:
        return (
            "Unknown model ID (HTTP 404). Check MODEL_SMART / MODEL_FAST (OpenRouter) or "
            "ANTHROPIC_MODEL_SMART / ANTHROPIC_MODEL_FAST (Anthropic / gateway) in .env."
        )
    return f"{type(exc).__name__}: {text}"[:300]


class LLMClient(Protocol):
    async def structured[T: BaseModel](
        self,
        task: Task,
        schema: type[T],
        messages: Sequence[BaseMessage],
        config: RunnableConfig | None = None,
    ) -> T: ...

    async def agent_structured[T: BaseModel](
        self,
        task: Task,
        schema: type[T],
        messages: Sequence[BaseMessage],
        tools: Sequence[BaseTool],
        config: RunnableConfig | None = None,
    ) -> tuple[T, list[dict[str, Any]]]: ...

    def stream_text(
        self, task: Task, messages: Sequence[BaseMessage], config: RunnableConfig | None = None
    ) -> AsyncIterator[str]: ...


class ClaudeLLM:
    """Production client (OpenRouter or Anthropic API). Structured output: see router.structured_methods."""

    def __init__(self) -> None:
        # model id -> the structured-output method that last worked for it. Tried first next time,
        # so a method the gateway does not support costs one failed round trip per process, not per call.
        self._working_method: dict[str, str] = {}

    def _methods_for(self, task: Task) -> list[str]:
        methods = structured_methods()
        preferred = self._working_method.get(model_id_for(task))
        return [preferred, *(m for m in methods if m != preferred)] if preferred in methods else methods

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
                if isinstance(result, schema):
                    self._working_method[model_id_for(task)] = method
                    return result
                try:
                    validated = schema.model_validate(result)
                except Exception as exc:
                    last_error = exc
                    msgs = [*msgs, AIMessage(content=f"Previous output was invalid: {exc}. Fix it.")]
                    continue
                self._working_method[model_id_for(task)] = method
                return validated
            log.info(
                "%s: structured method %s failed (%s), trying next", task, method, type(last_error).__name__
            )
        raise LLMError(f"{task}: {describe(last_error) if last_error else 'unknown error'}") from last_error

    async def agent_structured[T: BaseModel](
        self,
        task: Task,
        schema: type[T],
        messages: Sequence[BaseMessage],
        tools: Sequence[BaseTool],
        config: RunnableConfig | None = None,
    ) -> tuple[T, list[dict[str, Any]]]:
        """Bounded native tool-calling loop, then a final structured answer."""
        by_name = {t.name: t for t in tools}
        llm = get_chat_model(task).bind_tools(list(tools))
        msgs: list[BaseMessage] = list(messages)
        calls: list[dict[str, Any]] = []
        for _ in range(get_settings().llm_max_tool_steps):
            try:
                ai = await llm.ainvoke(msgs, config=config)
            except Exception as exc:
                raise LLMError(f"{task} tool loop: {describe(exc)}") from exc
            msgs.append(ai)
            if not isinstance(ai, AIMessage) or not ai.tool_calls:
                break
            for call in ai.tool_calls:
                calls.append({"name": call["name"], "args": call["args"]})
                tool = by_name.get(call["name"])
                output: Any = (
                    await tool.ainvoke(call["args"], config=config) if tool else {"error": "unknown tool"}
                )
                content = (
                    output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, default=str)
                )
                msgs.append(ToolMessage(content=content[:6000], tool_call_id=call["id"]))
        return await self.structured(task, schema, msgs, config), calls

    async def stream_text(
        self, task: Task, messages: Sequence[BaseMessage], config: RunnableConfig | None = None
    ) -> AsyncIterator[str]:
        # Runs inside a graph node, so LangGraph's "messages" stream mode also sees these tokens.
        model = get_chat_model(task, temperature=0.3, streaming=True)
        try:
            async for chunk in model.astream(list(messages), config=config):
                # Anthropic may send content as a list of blocks (text, thinking, ...); `.text`
                # joins only the text blocks, so thinking is never shown to the user.
                text = chunk.text
                if text:
                    yield text
        except Exception as exc:
            raise LLMError(f"{task} stream: {describe(exc)}") from exc


OpenRouterLLM = ClaudeLLM  # backwards-compatible name
