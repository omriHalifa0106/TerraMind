"""LLM-as-a-judge for faithfulness (needs OpenRouter). Claims in the answer vs the data it was given."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from terramind.llm.client import LLMClient

JUDGE_PROMPT = """You check a hiking assistant's answer for hallucinations.
You get DATA (the only facts the assistant was allowed to use) and the ANSWER.
List every factual claim in the ANSWER that is NOT supported by DATA (names, numbers, closures,
weather, distances, times). Style, tone and generic advice ("bring water") are not claims.
score = supported_claims / total_claims (1.0 if there are no factual claims)."""


class JudgeVerdict(BaseModel):
    unsupported_claims: list[str] = Field(default_factory=list)
    total_claims: int = Field(ge=0)
    score: float = Field(ge=0, le=1)


async def faithfulness(llm: LLMClient, output: dict[str, Any]) -> dict[str, Any]:
    if not output.get("summary") or output.get("path", [""])[-1] == "ask_clarification":
        return {}  # a clarification question is not an answer: nothing to judge
    verdict = await llm.structured(
        "judge",
        JudgeVerdict,
        [
            SystemMessage(content=JUDGE_PROMPT),
            HumanMessage(
                content=f"DATA: {json.dumps(output['data'], ensure_ascii=False, default=str)[:12000]}\n\n"
                f"ANSWER: {output['summary']}"
            ),
        ],
    )
    return {"faithfulness": round(verdict.score, 3), "unsupported": verdict.unsupported_claims}
