"""Check the OpenRouter connection: one FAST and one SMART structured call, with token usage.

uv run python -m scripts.smoke_llm
"""

from __future__ import annotations

import asyncio
import sys

from langchain_core.messages import HumanMessage
from pydantic import BaseModel

from terramind.config import get_settings
from terramind.llm.client import ClaudeLLM, LLMError
from terramind.llm.router import Task, model_id_for


class Ping(BaseModel):
    region: str
    is_water_trail: bool


async def main() -> int:
    s = get_settings()
    provider = s.resolved_llm_provider
    if not s.llm_key:
        print("No API key: set ANTHROPIC_AUTH_TOKEN (+ ANTHROPIC_BASE_URL) or OPENROUTER_API_KEY in .env")
        return 1
    print(
        f"provider: {provider}"
        + (f" via {s.anthropic_base_url}" if provider == "anthropic" and s.anthropic_base_url else "")
    )
    prompt = [HumanMessage("Extract: 'מסלול מים בגליל העליון'. region in English, is_water_trail bool.")]
    tasks: list[Task] = ["intake", "planner"]
    for task in tasks:
        try:
            parsed = await ClaudeLLM().structured(task, Ping, prompt)
        except LLMError as exc:
            print(f"[{task}] {model_id_for(task)}: FAILED - {exc}")
            return 1
        print(f"[{task}] {model_id_for(task)}: {parsed!r}")
    # Streaming (used by the explainer) is a separate code path: check it too.
    tokens = [
        t async for t in ClaudeLLM().stream_text("explainer", [HumanMessage("Say hello in Hebrew, 3 words.")])
    ]
    print(f"[explainer stream] {model_id_for('explainer')}: {''.join(tokens)!r} ({len(tokens)} chunks)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
