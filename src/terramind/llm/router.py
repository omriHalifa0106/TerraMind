"""Agent -> model tier -> chat model. The only place that knows model IDs.

Fast agents (extraction, tool selection) use Haiku; agents that reason or write for the user use
Sonnet. The Anthropic API is used directly, or through an Anthropic-compatible gateway when
ANTHROPIC_BASE_URL is set.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from langchain_anthropic import ChatAnthropic

from terramind.config import Settings, get_settings


class ModelTier(StrEnum):
    FAST = "fast"  # Haiku
    SMART = "smart"  # Sonnet


Task = Literal["request_analyst", "catalog_search", "route_planner", "presenter"]

TASK_TIERS: dict[Task, ModelTier] = {
    "request_analyst": ModelTier.FAST,  # agent 1: free text -> TripRequest
    "catalog_search": ModelTier.FAST,  # agent 2: choose the tool calls
    "route_planner": ModelTier.SMART,  # agent 4: choose the trail and stops
    "presenter": ModelTier.SMART,  # agent 5: write the explanation
}

# Structured-output strategies to try, in order. Forced tool calling first: in json_schema mode
# Haiku returned only the required fields and dropped every optional one (region, difficulty,
# group...), which then silently took their defaults. json_schema stays as the fallback.
StructuredMethod = Literal["json_schema", "function_calling"]
STRUCTURED_METHODS: list[StructuredMethod] = ["function_calling", "json_schema"]


def model_id_for(task: Task, s: Settings | None = None) -> str:
    s = s or get_settings()
    return s.anthropic_model_fast if TASK_TIERS[task] is ModelTier.FAST else s.anthropic_model_smart


def get_chat_model(task: Task, streaming: bool = False) -> ChatAnthropic:
    s = get_settings()
    headers = {"Authorization": f"Bearer {s.anthropic_auth_token}"} if s.anthropic_auth_token else None
    kwargs: dict[str, object] = {}
    if s.anthropic_base_url:
        kwargs["base_url"] = s.anthropic_base_url.rstrip("/")
    return ChatAnthropic(
        model=model_id_for(task, s),
        api_key=s.anthropic_auth_token or s.anthropic_api_key or "missing-key",
        # Newer Claude models reject a non-default temperature: leave it unset.
        timeout=s.llm_timeout_s,
        max_retries=s.llm_max_retries,
        max_tokens=s.anthropic_max_tokens,
        streaming=streaming,
        default_headers=headers,
        **kwargs,
    )
