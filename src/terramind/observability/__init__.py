"""LangSmith tracing setup.

LangSmith reads its settings from the process environment, while TerraMind reads `.env` through
pydantic-settings (which does not export anything). Without this step a local run
(`scripts.dev`) would never send traces, even with a valid key.
"""

from __future__ import annotations

import logging
import os

from terramind.config import Settings

log = logging.getLogger("terramind.observability")


def _real_key(key: str) -> bool:
    return len(key) > 20 and not key.endswith("...")  # not empty, not the .env.example placeholder


def configure_langsmith(s: Settings) -> bool:
    """Export the LangSmith settings so every graph run (all five agents, their LLM calls and tool
    calls) is traced. Returns whether tracing is on. Variables already set in the environment win."""
    if not s.langsmith_tracing:
        os.environ.setdefault("LANGSMITH_TRACING", "false")
        return False
    if not _real_key(s.langsmith_api_key):
        # Tracing with a placeholder key only produces 401s in the background: switch it off, loudly.
        log.warning("LANGSMITH_TRACING is on but LANGSMITH_API_KEY is missing: tracing disabled")
        os.environ["LANGSMITH_TRACING"] = "false"
        return False
    os.environ.setdefault("LANGSMITH_TRACING", "true")
    os.environ.setdefault("LANGSMITH_API_KEY", s.langsmith_api_key)
    os.environ.setdefault("LANGSMITH_PROJECT", s.langsmith_project)
    if s.langsmith_endpoint:
        os.environ.setdefault("LANGSMITH_ENDPOINT", s.langsmith_endpoint)
    log.info("LangSmith tracing on (project %s)", os.environ["LANGSMITH_PROJECT"])
    return True
