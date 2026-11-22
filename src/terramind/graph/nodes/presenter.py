"""Agent 5 - presentation: explain the result to the hiker, streamed token by token.

It gathers everything the earlier agents produced into one DATA block (the request, the chosen
trail and why, the plan, the alternatives, every veto with its evidence, the weather and the
retrieved tips) and Claude (Sonnet) writes the answer in the user's language. The prompt only
allows facts from DATA, and tips are cited by their chunk id.

"Why this and not that" is not invented by the model: each trade-off comes from a veto that agent 3
recorded, with the tool data behind it.
"""

from __future__ import annotations

import re
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.runtime import Runtime

from terramind.graph.nodes.common import emit, error, is_hebrew, last_user_text, prompt, to_json
from terramind.graph.state import TerraMindContext, TerraMindState
from terramind.llm.caching import cached_system
from terramind.llm.client import LLMError
from terramind.schemas import CandidateAssessment, FinalAnswer, RoutePlan, Severity, TradeOff, TrailCandidate

CITATION = re.compile(r"\[(kc-[0-9a-f]{16})\]")


def tradeoffs_from(assessments: list[CandidateAssessment], plan: RoutePlan | None) -> list[TradeOff]:
    """One trade-off per (vetoed trail, reason), each backed by the evidence agent 3 recorded."""
    if plan is None:
        return []
    out: dict[tuple[str, str], TradeOff] = {}
    for a in assessments:
        if a.approved:
            continue
        for issue in a.issues:
            key = (issue.trail_id, issue.code)
            if issue.severity is Severity.CRITICAL and key not in out:
                out[key] = TradeOff(
                    chosen_trail_id=plan.trail_id,
                    rejected_trail_id=issue.trail_id,
                    rejected_trail_name=issue.trail_name,
                    factor=issue.code,
                    detail=issue.evidence,
                )
    return list(out.values())


def veto_reasons(assessments: list[CandidateAssessment]) -> list[str]:
    return sorted(
        {
            f"{i.trail_name or i.trail_id}: {i.evidence}"
            for a in assessments
            if not a.approved
            for i in a.issues
            if i.severity is Severity.CRITICAL
        }
    )


def template_summary(final: FinalAnswer, he: bool) -> str:
    """Used when the model is unavailable: plain but correct."""
    if final.plan is None:
        head = "לא מצאתי מסלול בטוח לבקשה הזו." if he else "I could not find a safe route for this request."
        return head + "\n\n" + "\n".join(f"- {n}" for n in final.safety_notes)
    p = final.plan
    lines = [f"**{p.title}**: {p.start_time:%H:%M}-{p.end_time:%H:%M}, {p.walk_km:.1f} km"]
    lines += [f"{s.order}. {s.name} ({s.arrive_at:%H:%M})" for s in p.stops if s.arrive_at]
    lines += [f"- {t.rejected_trail_name or t.rejected_trail_id}: {t.detail}" for t in final.tradeoffs]
    lines += [f"- {n}" for n in final.safety_notes]
    return "\n".join(lines)


def _facts(c: TrailCandidate) -> dict[str, Any]:
    return {
        "trail_id": c.trail_id,
        "name": c.name_he or c.name,
        "length_km": c.length_km,
        "difficulty": c.difficulty,
        "tags": c.tags,
        "is_loop": c.is_loop,
    }


async def presenter(state: TerraMindState, runtime: Runtime[TerraMindContext]) -> dict[str, Any]:
    emit(runtime, "node", node="presenter", status="start")
    user_text = last_user_text(state["messages"])
    plan = state.get("plan")
    assessments = state.get("assessments", [])
    chosen = next((a for a in assessments if plan and a.trail_id == plan.trail_id and a.approved), None)
    candidates = state.get("candidates", [])

    notes = [i.evidence for i in (chosen.issues if chosen else []) if i.severity is not Severity.CRITICAL]
    if plan is None:
        notes = veto_reasons(assessments) or [
            "No trail in the database matched the request, even after relaxing the filters."
        ]
    evidence = [e for e in state.get("evidence", []) if plan is None or e.trail_id in (plan.trail_id, None)]
    final = FinalAnswer(
        summary_md="",
        plan=plan,
        tradeoffs=tradeoffs_from(assessments, plan),
        safety_notes=[*notes, *state.get("assumptions", [])],
        tips=[e.text for e in evidence[:3]],
    )
    req = state.get("request")
    data = {
        # Only what the user said: defaults (2 adults, 3.5 km/h) are not facts about the hiker.
        "request": req.model_dump(
            mode="json", exclude_defaults=True, exclude={"semantic_query", "missing_fields"}
        )
        if req
        else None,
        "chosen_trail": next((_facts(c) for c in candidates if plan and c.trail_id == plan.trail_id), None),
        "chosen_because": state.get("plan_rationale"),
        "alternatives": [_facts(c) for c in candidates if not plan or c.trail_id != plan.trail_id][:4],
        "plan": plan,
        "tradeoffs": final.tradeoffs,
        "safety_notes": final.safety_notes,
        "weather": chosen.weather if chosen else None,
        "evidence": [{"id": e.chunk_id, "text": e.text} for e in evidence[:6]],
    }

    errors = []
    try:
        parts: list[str] = []
        async for token in runtime.context.llm.stream_text(
            "presenter",
            [
                cached_system(prompt("presenter")),
                HumanMessage(content=f"DATA: {to_json(data)}\n\nUSER MESSAGE: {user_text}"),
            ],
        ):
            parts.append(token)
            emit(runtime, "token", node="presenter", text=token)
        text = "".join(parts)
    except LLMError as exc:
        text = template_summary(final, is_hebrew(user_text))
        errors.append(error("presenter", "llm_error", exc))

    valid = {e.chunk_id for e in evidence}
    cited = [c for c in dict.fromkeys(CITATION.findall(text)) if c in valid]
    final = final.model_copy(update={"summary_md": text, "cited_chunk_ids": cited})
    emit(runtime, "final", node="presenter", final=final.model_dump(mode="json"))
    update: dict[str, Any] = {"final": final, "messages": [AIMessage(content=text)]}
    if errors:
        update["errors"] = errors
    return update
