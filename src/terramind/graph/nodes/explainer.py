"""Explainer: final answer with causal trade-offs (Explainable Routing), streamed token by token."""

from __future__ import annotations

import re
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.runtime import Runtime

from terramind.graph.nodes.common import emit, error, is_hebrew, last_user_text, prompt, to_json
from terramind.graph.state import TerraMindContext, TerraMindState
from terramind.llm.caching import cached_system
from terramind.llm.client import LLMError
from terramind.schemas import FinalAnswer, RoutePlan, SafetyVerdict, Severity, TradeOff, TrailCandidate

CITATION = re.compile(r"\[(kc-[0-9a-f]{16})\]")


def tradeoffs_from(history: list[SafetyVerdict], plan: RoutePlan | None) -> list[TradeOff]:
    """Derived from the critic's verdicts, not generated: every trade-off is backed by tool data."""
    out: dict[tuple[str, str], TradeOff] = {}
    if plan is None:
        return []
    for v in history:
        if v.approved or v.replan_scope != "trail":
            continue
        for issue in v.issues:
            key = (issue.trail_id, issue.code)
            if issue.severity is Severity.CRITICAL and issue.trail_id != plan.trail_id and key not in out:
                out[key] = TradeOff(
                    chosen_trail_id=plan.trail_id,
                    rejected_trail_id=issue.trail_id,
                    rejected_trail_name=issue.trail_name,
                    factor=issue.code,
                    detail=issue.evidence,
                )
    return list(out.values())


def template_summary(final: FinalAnswer, he: bool) -> str:
    """Used when the explainer model is unavailable: plain but correct."""
    if final.plan is None:
        head = "לא מצאתי מסלול בטוח לבקשה הזו." if he else "I could not find a safe route for this request."
        return head + "\n\n" + "\n".join(f"- {n}" for n in final.safety_notes)
    p = final.plan
    lines = [f"**{p.title}**: {p.start_time:%H:%M}-{p.end_time:%H:%M}, {p.walk_km:.1f} km"]
    lines += [f"{s.order}. {s.name} ({s.arrive_at:%H:%M})" for s in p.stops if s.arrive_at]
    lines += [f"- {t.rejected_trail_name or t.rejected_trail_id}: {t.detail}" for t in final.tradeoffs]
    lines += [f"- {n}" for n in final.safety_notes]
    return "\n".join(lines)


async def _stream(runtime: Runtime[TerraMindContext], messages: list[Any]) -> str:
    parts: list[str] = []
    async for token in runtime.context.llm.stream_text("explainer", messages):
        parts.append(token)
        emit(runtime, "token", node="explainer", text=token)
    return "".join(parts)


async def explainer(state: TerraMindState, runtime: Runtime[TerraMindContext]) -> dict[str, Any]:
    emit(runtime, "node", node="explainer", status="start")
    user_text = last_user_text(state["messages"])
    he = is_hebrew(user_text)

    # --- follow-up question about an existing plan ---
    if state.get("intent") == "question" and state.get("final") is not None:
        final = state["final"]
        assert final is not None
        try:
            text = await _stream(
                runtime,
                [
                    cached_system(prompt("explainer")),
                    HumanMessage(
                        content=f"DATA: {to_json(final)}\nQUESTION: {user_text}\n"
                        "Answer the question only, using the DATA."
                    ),
                ],
            )
        except LLMError as exc:
            return {
                "messages": [AIMessage(content=template_summary(final, he))],
                "errors": [error("explainer", "llm_error", exc)],
            }
        return {"messages": [AIMessage(content=text)]}

    # --- final answer for a new / refined plan ---
    verdict = state.get("verdict")
    plan = state.get("draft_plan") if verdict is not None and verdict.approved else None
    history = state.get("verdict_history", [])
    base = state.get("final") if plan is None else None  # safe_fallback already wrote the reasons
    evidence = [e for e in state.get("evidence", []) if plan is None or e.trail_id in (plan.trail_id, None)]
    warnings = [i.evidence for i in (verdict.issues if verdict else []) if i.severity is Severity.WARNING]
    final = FinalAnswer(
        summary_md="",
        plan=plan,
        tradeoffs=tradeoffs_from(history, plan),
        safety_notes=[*(base.safety_notes if base else []), *warnings, *state.get("assumptions", [])],
        tips=[e.text for e in evidence[:3]],
    )
    timing_changes = [v.guidance_for_replan for v in history if v.replan_scope == "timing"]
    req = state.get("request")
    chosen_id = plan.trail_id if plan else None

    def facts(c: TrailCandidate) -> dict[str, Any]:
        return {
            "trail_id": c.trail_id,
            "name": c.name_he or c.name,
            "length_km": c.length_km,
            "difficulty": c.difficulty,
            "tags": c.tags,
            "is_loop": c.is_loop,
            "distance_km": c.distance_km,
            "match_score": c.fused_score,
        }

    candidates = state.get("candidates", [])
    chosen = next((facts(c) for c in candidates if c.trail_id == chosen_id), None)
    alternatives = [facts(c) for c in candidates if c.trail_id != chosen_id][:4]
    data = {
        # Only what the user actually said: defaults (2 adults, medium fitness, 3.5 km/h) are system
        # assumptions, and presenting them as facts about the hiker was flagged by the judge.
        "request": req.model_dump(
            mode="json", exclude_defaults=True, exclude={"semantic_query", "missing_fields"}
        )
        if req
        else None,
        "chosen_trail": chosen,
        "chosen_because": state.get("plan_rationale"),
        "alternatives": alternatives,
        "plan": final.plan,
        "tradeoffs": final.tradeoffs,
        "safety_notes": final.safety_notes,
        "start_time_changes": timing_changes,
        "weather": state.get("weather"),
        "evidence": [{"id": e.chunk_id, "text": e.text} for e in evidence[:6]],
    }
    errors = []
    try:
        text = await _stream(
            runtime,
            [
                cached_system(prompt("explainer")),
                HumanMessage(content=f"DATA: {to_json(data)}\n\nUSER MESSAGE: {user_text}"),
            ],
        )
    except LLMError as exc:
        text = template_summary(final, he)
        errors.append(error("explainer", "llm_error", exc))
    valid_ids = {e.chunk_id for e in evidence}
    cited = [c for c in dict.fromkeys(CITATION.findall(text)) if c in valid_ids]
    final = final.model_copy(update={"summary_md": text, "cited_chunk_ids": cited})
    emit(runtime, "final", node="explainer", final=final.model_dump(mode="json"))
    update: dict[str, Any] = {"final": final, "messages": [AIMessage(content=text)]}
    if errors:
        update["errors"] = errors
    return update
