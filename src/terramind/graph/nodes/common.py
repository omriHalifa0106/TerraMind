"""Helpers shared by the agents: prompts, stream events, error records, JSON rendering."""

from __future__ import annotations

import contextlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from langchain_core.messages import AnyMessage, HumanMessage
from langgraph.runtime import Runtime
from pydantic import BaseModel

from terramind.graph.state import TerraMindContext
from terramind.schemas import NodeError

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
HEBREW = re.compile(r"[֐-׿]")


@lru_cache(maxsize=8)
def prompt(name: str) -> str:
    """The system prompt of an agent, from graph/prompts/<name>.md."""
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def emit(runtime: Runtime[TerraMindContext], event: str, **data: Any) -> None:
    """Send a progress event to the browser (LangGraph stream_mode="custom" -> SSE)."""
    with contextlib.suppress(Exception):  # streaming is best-effort; never break an agent over it
        runtime.stream_writer({"event": event, **data})


def error(node: str, kind: str, exc: BaseException | str) -> NodeError:
    message = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
    return NodeError.model_validate(
        {"node": node, "kind": kind, "message": message[:500], "recoverable": True}
    )


def to_json(obj: Any) -> str:
    def default(o: Any) -> Any:
        return o.model_dump(mode="json") if isinstance(o, BaseModel) else str(o)

    return json.dumps(obj, ensure_ascii=False, default=default)


def last_user_text(messages: list[AnyMessage]) -> str:
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            return m.content if isinstance(m.content, str) else str(m.content)
    return ""


def is_hebrew(text: str) -> bool:
    return bool(HEBREW.search(text))
