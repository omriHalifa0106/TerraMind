"""Anthropic prompt caching: mark the static system prompt of an agent as cacheable."""

from __future__ import annotations

from langchain_core.messages import SystemMessage


def cached_system(text: str) -> SystemMessage:
    """A system message whose (static) text is cached by Anthropic between calls.

    Keep the static instructions first and the volatile context (today's date, the request) in a
    later message, because the cache matches on the prompt prefix.
    """
    return SystemMessage(content=[{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}])
