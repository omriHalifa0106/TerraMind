"""Anthropic prompt caching through OpenRouter: mark static system prompts as cacheable."""

from __future__ import annotations

from langchain_core.messages import SystemMessage


def cached_system(text: str) -> SystemMessage:
    """A system message whose (static) text block is cached by Anthropic between calls.

    Keep the order stable: instructions -> tool docs -> examples first, volatile context last,
    because the cache matches on the prompt prefix.
    """
    return SystemMessage(content=[{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}])
