"""Task -> model tier -> chat model. The only place that knows providers and model IDs.

Providers: OpenRouter (OpenAI-compatible, server-side model fallbacks) or the native Anthropic
Messages API, directly or through an Anthropic-compatible gateway (ANTHROPIC_BASE_URL).
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Literal

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from terramind.config import Settings, get_settings


class ModelTier(StrEnum):
    FAST = "fast"  # Haiku: routing, extraction, grading, soft safety review
    SMART = "smart"  # Sonnet: planning, explanation, judging


Task = Literal["supervisor", "intake", "grader", "planner", "safety_critic", "explainer", "judge"]

TASK_TIERS: dict[Task, ModelTier] = {
    "supervisor": ModelTier.FAST,
    "intake": ModelTier.FAST,
    "grader": ModelTier.FAST,
    "planner": ModelTier.SMART,
    # Haiku: the critic only adds soft warnings on top of the deterministic rules (it cannot veto),
    # so the faster model is enough and it runs on every attempt of the safety loop.
    "safety_critic": ModelTier.FAST,
    "explainer": ModelTier.SMART,
    "judge": ModelTier.SMART,
}


def _build(
    model_id: str, fallbacks: list[str], s: Settings, temperature: float, streaming: bool
) -> ChatOpenAI:
    return ChatOpenAI(
        model=model_id,
        base_url=s.openrouter_base_url,
        api_key=s.openrouter_api_key or "missing-key",  # fail at call time with a clear 401
        temperature=temperature,
        timeout=s.llm_timeout_s,
        max_retries=s.llm_max_retries,
        streaming=streaming,
        stream_usage=True,
        default_headers={"HTTP-Referer": s.app_url, "X-Title": "TerraMind"},
        # Server-side fallback: OpenRouter tries these models in order if the primary fails.
        extra_body={"models": [model_id, *fallbacks]},
    )


def _build_anthropic(model_id: str, s: Settings, streaming: bool) -> ChatAnthropic:
    token = s.anthropic_auth_token or s.anthropic_api_key
    headers = {"Authorization": f"Bearer {s.anthropic_auth_token}"} if s.anthropic_auth_token else None
    kwargs: dict[str, object] = {}
    if s.anthropic_base_url:
        kwargs["base_url"] = s.anthropic_base_url.rstrip("/")
    return ChatAnthropic(
        model=model_id,
        api_key=token or "missing-key",
        # Newer Claude models (e.g. Sonnet 5.5) reject non-default temperature: leave it unset.
        timeout=s.llm_timeout_s,
        max_retries=s.llm_max_retries,
        max_tokens=s.anthropic_max_tokens,
        streaming=streaming,
        default_headers=headers,
        **kwargs,
    )


def model_id_for(task: Task, s: Settings | None = None) -> str:
    s = s or get_settings()
    fast = TASK_TIERS[task] is ModelTier.FAST
    if s.resolved_llm_provider == "anthropic":
        return s.anthropic_model_fast if fast else s.anthropic_model_smart
    return s.model_fast if fast else s.model_smart


@lru_cache(maxsize=32)
def structured_methods() -> list[str]:
    """Structured-output strategies to try, in order, for the active provider.

    Anthropic: native structured outputs (json_schema) first; forced tool calling is not
    supported on every model (e.g. Sonnet 5.5). OpenRouter: tool calling is the portable path.
    """
    if get_settings().resolved_llm_provider == "anthropic":
        return ["json_schema", "function_calling"]
    return ["function_calling"]


def get_chat_model(task: Task, temperature: float = 0.0, streaming: bool = False) -> BaseChatModel:
    s = get_settings()
    model_id = model_id_for(task, s)
    if s.resolved_llm_provider == "anthropic":
        return _build_anthropic(model_id, s, streaming)
    fallbacks = s.model_fast_fallbacks if TASK_TIERS[task] is ModelTier.FAST else s.model_smart_fallbacks
    return _build(model_id, fallbacks, s, temperature, streaming)
