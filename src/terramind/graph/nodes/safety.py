"""Safety & feasibility critic: deterministic rules first, Sonnet for soft concerns only."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime

from terramind.graph.nodes.common import emit, error, prompt, to_json
from terramind.graph.state import TerraMindContext, TerraMindState
from terramind.llm.caching import cached_system
from terramind.llm.client import LLMError
from terramind.safety.rules import evaluate, verdict_from
from terramind.schemas import FinalAnswer, LlmSafetyReview, SafetyIssue, Severity

TIMING_SHIFT_MINUTES = 90


async def safety_critic(state: TerraMindState, runtime: Runtime[TerraMindContext]) -> dict[str, Any]:
    plan = state["draft_plan"]
    assert plan is not None
    req = state["request"]
    trail = next(c for c in state.get("candidates", []) if c.trail_id == plan.trail_id)
    weather = state.get("weather")
    emit(runtime, "node", node="safety_critic", status="start", trail_id=plan.trail_id)

    issues = evaluate(plan, trail, req, weather, state.get("trail_status"), state.get("alerts", []))
    verdict = verdict_from(issues, plan, weather)
    errors = []

    if verdict.approved:
        # Only plans the rules already accept reach the LLM. It can add warnings, never approve
        # a vetoed plan and never veto on its own: that keeps the safety loop testable.
        try:
            review = await runtime.context.llm.structured(
                "safety_critic",
                LlmSafetyReview,
                [
                    cached_system(prompt("safety_critic")),
                    HumanMessage(
                        content=(
                            f"PLAN: {to_json(plan)}\nTRAIL: {to_json(trail)}\nGROUP: {to_json(req.group)}\n"
                            f"WEATHER: {to_json(weather)}\nALERTS: {to_json(state.get('alerts', []))}\n"
                            f"EVIDENCE: {to_json([e.text for e in state.get('evidence', [])][:6])}\n"
                            f"RULE ISSUES: {to_json(issues)}"
                        )
                    ),
                ],
            )
            extra = [
                SafetyIssue(
                    trail_id=plan.trail_id,
                    trail_name=trail.name_he or trail.name,
                    code=i.code,
                    severity=Severity.WARNING,
                    evidence=i.evidence,
                    source="llm",
                )
                for i in review.extra_issues
            ]
            verdict = verdict.model_copy(update={"issues": [*verdict.issues, *extra]})
        except LLMError as exc:
            errors.append(error("safety_critic", "llm_error", exc))

    attempts = state.get("attempts", 0) + 1
    shift = state.get("start_shift_minutes", 0)
    emit(
        runtime,
        "verdict",
        node="safety_critic",
        attempt=attempts,
        approved=verdict.approved,
        scope=verdict.replan_scope,
        issues=[i.model_dump(mode="json") for i in verdict.issues],
    )
    update: dict[str, Any] = {
        "verdict": verdict,
        "verdict_history": [verdict],
        "rejected_trail_ids": verdict.rejected_trail_ids,
        "attempts": attempts,
        "start_shift_minutes": shift + TIMING_SHIFT_MINUTES if verdict.replan_scope == "timing" else 0,
    }
    if errors:
        update["errors"] = errors
    return update


async def safe_fallback(state: TerraMindState, runtime: Runtime[TerraMindContext]) -> dict[str, Any]:
    """No safe option found: return no plan (never a guessed one) and let the explainer say why."""
    emit(runtime, "node", node="safe_fallback", status="start")
    reasons = sorted(
        {
            f"{i.trail_name or i.trail_id}: {i.evidence}"
            for v in state.get("verdict_history", [])
            for i in v.issues
            if i.severity is Severity.CRITICAL
        }
    )
    if not reasons and not state.get("candidates"):
        reasons = ["No trail in the database matched the request, even after relaxing the filters."]
    final = FinalAnswer(summary_md="", plan=None, safety_notes=reasons)
    return {"final": final, "draft_plan": None}
