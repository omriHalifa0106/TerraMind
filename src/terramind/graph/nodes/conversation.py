"""Turn-level nodes: load_profile, supervisor, intake, ask_clarification, save_memory."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Literal

from langchain_core.messages import AIMessage, SystemMessage
from langgraph.runtime import Runtime
from pydantic import BaseModel, Field

from terramind.graph.nodes.common import emit, error, is_hebrew, last_user_text, prompt, to_json
from terramind.graph.state import TerraMindContext, TerraMindState, per_turn_reset
from terramind.llm.caching import cached_system
from terramind.llm.client import LLMError
from terramind.llm.heuristic import extract_request
from terramind.regions import region_of
from terramind.schemas import GroupProfile, TrailCandidate, TripRequest, UserProfile
from terramind.schemas.tool_results import TrailSearchResult

PROFILE_NAMESPACE = "users"
PROFILE_KEY = "profile"
FULL_NAME_MATCH = 1.0


class RouteDecision(BaseModel):
    intent: Literal["new_plan", "refine_plan", "question", "smalltalk"]
    reason: str = Field(max_length=300)


async def load_profile(state: TerraMindState, runtime: Runtime[TerraMindContext]) -> dict[str, Any]:
    emit(runtime, "node", node="load_profile", status="start")
    update: dict[str, Any] = per_turn_reset()
    if runtime.store is not None:
        item = await runtime.store.aget((PROFILE_NAMESPACE, state["user_id"]), PROFILE_KEY)
        if item is not None:
            update["profile"] = UserProfile.model_validate(item.value)
    return update


async def supervisor(state: TerraMindState, runtime: Runtime[TerraMindContext]) -> dict[str, Any]:
    """Classify the turn once (Haiku); later passes are pure rules (see routing.route_supervisor)."""
    if state.get("turn_classified"):
        return {}
    emit(runtime, "node", node="supervisor", status="start")
    has_plan = state.get("final") is not None
    context = f"EXISTING PLAN: {'yes' if has_plan else 'no'}"
    recent = state["messages"][-6:]
    try:
        decision = await runtime.context.llm.structured(
            "supervisor",
            RouteDecision,
            [cached_system(prompt("supervisor")), SystemMessage(content=context), *recent],
        )
        intent = decision.intent
        if intent in ("question", "refine_plan") and not has_plan:
            intent = "new_plan"
    except LLMError as exc:
        emit(
            runtime,
            "warning",
            node="supervisor",
            detail=f"router unavailable, assuming new_plan: {exc}"[:300],
        )
        return {
            "intent": "new_plan",
            "turn_classified": True,
            "errors": [error("supervisor", "llm_error", exc)],
        }
    emit(runtime, "route", node="supervisor", intent=intent)
    return {"intent": intent, "turn_classified": True}


def _apply_defaults(
    req: TripRequest, profile: UserProfile | None, today: date
) -> tuple[TripRequest, list[str]]:
    notes: list[str] = []
    updates: dict[str, Any] = {}
    if req.trip_date is None:
        updates["trip_date"] = today + timedelta(days=1)
        notes.append(f"No date given: planned for tomorrow ({updates['trip_date']:%Y-%m-%d}).")
    if profile is not None:
        if req.group == GroupProfile() and profile.default_group is not None:
            updates["group"] = profile.default_group
            notes.append("Used the group from your saved preferences.")
        if req.difficulty is None and profile.preferred_difficulty is not None:
            updates["difficulty"] = profile.preferred_difficulty
            notes.append(f"Used your usual difficulty ({profile.preferred_difficulty.value}).")
    missing = list(req.missing_fields)
    if req.region and "region" in missing:
        missing.remove("region")
    if not req.region and "region" not in missing:
        missing.append("region")
    updates["missing_fields"] = [m for m in missing if m == "region"]  # only region blocks planning
    return req.model_copy(update=updates), notes


async def _named_trails(runtime: Runtime[TerraMindContext], text: str) -> list[TrailCandidate]:
    """Trails the user named in full ("לטייל בנחל כזיב"). Partial matches (one shared word of a
    long name, e.g. "גליל עליון" in "שביל אזורי גליל עליון") are ignored."""
    raw = await runtime.context.tools.call("gis", "find_trails_by_name", {"text": text, "limit": 3})
    try:
        found = TrailSearchResult.model_validate(raw).trails
    except ValueError:
        return []
    return [t for t in found if t.fused_score >= FULL_NAME_MATCH]


def _fill_gaps(req: TripRequest, text: str, today: date) -> TripRequest:
    """The LLM sometimes leaves obvious fields empty ("ליד מירון" -> no region). Fill only the
    empty ones from keyword extraction; never override what the LLM did extract."""
    kw = extract_request(text, today)
    update: dict[str, Any] = {}
    if not req.region and kw.region:
        update["region"] = kw.region
    if not req.trail_types and kw.trail_types:
        update["trail_types"] = kw.trail_types
    if req.difficulty is None and kw.difficulty is not None:
        update["difficulty"] = kw.difficulty
    return req.model_copy(update=update) if update else req


async def intake(state: TerraMindState, runtime: Runtime[TerraMindContext]) -> dict[str, Any]:
    emit(runtime, "node", node="intake", status="start")
    today = date.today()
    previous = state.get("request") if state.get("intent") == "refine_plan" else None
    profile = state.get("profile")
    volatile = (
        f"TODAY: {today.isoformat()} ({today:%A})\n"
        f"PREVIOUS REQUEST: {to_json(previous) if previous else 'none'}\n"
        f"SAVED PREFERENCES: {to_json(profile) if profile else 'none'}"
    )
    llm_errors = []
    try:
        req = await runtime.context.llm.structured(
            "intake",
            TripRequest,
            [cached_system(prompt("intake")), SystemMessage(content=volatile), *state["messages"][-6:]],
        )
    except LLMError as exc:
        # Degrade, don't stop: keyword extraction keeps the pipeline (and the safety loop) running.
        req = extract_request(last_user_text(state["messages"]), today)
        llm_errors.append(error("intake", "llm_error", exc))
        emit(
            runtime,
            "warning",
            node="intake",
            detail=f"Claude unavailable, using keyword extraction: {exc}"[:300],
        )
    text = last_user_text(state["messages"])
    req = _fill_gaps(req, text, today)
    named = await _named_trails(runtime, text)
    inferred_from: str | None = None
    if not req.region and named and (region := region_of(named[0].trailhead)) is not None:
        req = req.model_copy(update={"region": region.name_en})
        inferred_from = named[0].name_he or named[0].name
    req, notes = _apply_defaults(req, profile, today)
    if inferred_from:
        notes.append(f"Region inferred from the trail you named ({inferred_from}).")
    emit(runtime, "request", node="intake", request=req.model_dump(mode="json"))
    update: dict[str, Any] = {
        "request": req,
        "intake_done": True,
        "assumptions": notes,
        "named_trail_ids": [t.trail_id for t in named],
    }
    if llm_errors:
        update["errors"] = llm_errors
    return update


async def ask_clarification(state: TerraMindState, runtime: Runtime[TerraMindContext]) -> dict[str, Any]:
    emit(runtime, "node", node="ask_clarification", status="start")
    he = is_hebrew(last_user_text(state["messages"]))
    if state.get("intent") == "smalltalk":
        text = (
            "היי! אני TerraMind. ספרו לי איפה תרצו לטייל (גליל עליון, מירון או הגולן), "
            "מתי ועם מי, ואציע מסלול."
            if he
            else "Hi! I'm TerraMind. Tell me where you'd like to hike (Upper Galilee, Meron or the Golan), "
            "when, and with whom, and I'll suggest a route."
        )
    else:
        text = (
            "באיזה אזור תרצו לטייל? אני מכיר כרגע את הגליל העליון, אזור מירון ורמת הגולן."
            if he
            else "Which area would you like to hike in? "
            "I currently cover the Upper Galilee, the Meron area and the Golan."
        )
    return {"messages": [AIMessage(content=text)]}


async def save_memory(state: TerraMindState, runtime: Runtime[TerraMindContext]) -> dict[str, Any]:
    """Persist durable preferences (not the whole conversation) in the cross-thread Store."""
    req = state.get("request")
    if runtime.store is None or req is None or state.get("intent") == "question":
        return {}
    old = state.get("profile") or UserProfile(user_id=state["user_id"])
    profile = old.model_copy(
        update={
            "preferred_difficulty": req.difficulty or old.preferred_difficulty,
            "favorite_types": list(dict.fromkeys([*old.favorite_types, *req.trail_types]))[-6:],
            "default_group": req.group if req.group != GroupProfile() else old.default_group,
        }
    )
    await runtime.store.aput(
        (PROFILE_NAMESPACE, state["user_id"]), PROFILE_KEY, profile.model_dump(mode="json")
    )
    emit(runtime, "node", node="save_memory", status="done")
    return {"profile": profile}


__all__ = ["RouteDecision", "ask_clarification", "intake", "load_profile", "save_memory", "supervisor"]
