"""Agent 1 - request analysis.

Takes what the user wrote ("בא לי מסלול מים בצפון") and turns it into a structured TripRequest:
region, date, difficulty, trail types, group. Claude (Haiku) does the extraction; code then fills
the gaps it can verify (the region of a trail the user named, tomorrow as the default date).
If no region can be found, the agent asks the user instead of guessing, and the run ends.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from langchain_core.messages import AIMessage, SystemMessage
from langgraph.runtime import Runtime
from pydantic import BaseModel, Field

from terramind.graph.nodes.common import emit, error, is_hebrew, last_user_text, prompt
from terramind.graph.scope import Change, decide_scope
from terramind.graph.state import TerraMindContext, TerraMindState
from terramind.llm.caching import cached_system
from terramind.llm.client import LLMError
from terramind.regions import region_of, resolve_region
from terramind.schemas import PreviousTurn, TrailCandidate, TripRequest
from terramind.schemas.tool_results import TrailSearchResult

FULL_NAME_MATCH = 1.0  # find_trails_by_name score for a trail named in full


async def _named_trails(runtime: Runtime[TerraMindContext], text: str) -> list[TrailCandidate]:
    """Trails the user named in full ("לטייל בנחל כזיב"). Partial matches (one shared word of a
    long name, e.g. "גליל עליון" in "שביל אזורי גליל עליון") are ignored."""
    raw = await runtime.context.tools.call("gis", "find_trails_by_name", {"text": text, "limit": 3})
    try:
        found = TrailSearchResult.model_validate(raw).trails
    except ValueError:
        return []
    return [t for t in found if t.fused_score >= FULL_NAME_MATCH]


class AnalystOutput(BaseModel):
    """Agent 1's structured answer: the (updated) request and what kind of message this was."""

    request: TripRequest
    change: Change = Field(
        default="new_trip",
        description="new_trip: a new or unrelated trip. edit: changes the current trip "
        "(hours, places, start, date...). different_trail: wants another trail for the same trip.",
    )


def _conversation(previous: PreviousTurn) -> str:
    """The previous turn, for a follow-up message ("make it 4 hours", "add a winery")."""
    plan = previous.plan
    current = (
        {
            "trail": plan.title,
            "start": str(plan.start_time)[:5],
            "end": str(plan.day_end_time or plan.end_time)[:5],
            "stops": [s.name for s in plan.stops],
        }
        if plan
        else None
    )
    chat = "\n".join(f"{line.role}: {line.text[:600]}" for line in previous.history)
    return (
        f"PREVIOUS REQUEST: {previous.request.model_dump_json()}\n"
        f"CURRENT PLAN: {json.dumps(current, ensure_ascii=False)}\n"
        f"RECENT CHAT:\n{chat}"
    )


def _ask_for_region(he: bool) -> str:
    if he:
        return "באיזה אזור תרצו לטייל? אני מכיר כרגע את הגליל העליון, אזור מירון ורמת הגולן."
    return (
        "Which area would you like to hike in? "
        "I currently cover the Upper Galilee, the Meron area and the Golan."
    )


async def request_analyst(state: TerraMindState, runtime: Runtime[TerraMindContext]) -> dict[str, Any]:
    emit(runtime, "node", node="request_analyst", status="start")
    text = last_user_text(state["messages"])
    he = is_hebrew(text)
    today = date.today()

    previous = state.get("previous")
    try:
        out = await runtime.context.llm.structured(
            "request_analyst",
            AnalystOutput,
            [
                cached_system(prompt("request_analyst")),
                SystemMessage(content=f"TODAY: {today.isoformat()} ({today:%A})"),
                *([SystemMessage(content=_conversation(previous))] if previous else []),
                *state["messages"][-1:],
            ],
        )
    except LLMError as exc:
        msg = (
            "השירות לא זמין כרגע, נסו שוב בעוד רגע."
            if he
            else "The service is unavailable, please try again."
        )
        return {
            "clarification": msg,
            "messages": [AIMessage(content=msg)],
            "errors": [error("request_analyst", "llm_error", exc)],
        }

    req, change = out.request, out.change
    if previous is not None and change != "new_trip":
        # A follow-up keeps what it does not change, even if the model left a field out.
        keep = {k: getattr(previous.request, k) for k in ("region", "trip_date") if getattr(req, k) is None}
        req = req.model_copy(update=keep)

    notes: list[str] = []
    named = await _named_trails(runtime, text)
    if not req.region and (found := resolve_region(text)) is not None:
        req = req.model_copy(update={"region": found.name_en})
    if not req.region and named and (found := region_of(named[0].trailhead)) is not None:
        req = req.model_copy(update={"region": found.name_en})
        notes.append(f"Region inferred from the trail you named ({named[0].name_he or named[0].name}).")
    if (origin := state.get("user_origin")) is not None:
        # Chosen in the UI, so it is exact: it overrides anything the model guessed from the text.
        req = req.model_copy(
            update={"origin": (origin["lat"], origin["lon"]), "origin_label": origin["label"]}
        )
    if req.day_hours is None and (hours := state.get("user_day_hours")):
        req = req.model_copy(update={"day_hours": hours})  # the UI picker; a length in the text wins
    if req.trip_date is None:
        req = req.model_copy(update={"trip_date": today + timedelta(days=1)})
        notes.append(f"No date given: planned for tomorrow ({today + timedelta(days=1):%Y-%m-%d}).")
    req = req.model_copy(update={"missing_fields": [] if req.region else ["region"]})

    emit(runtime, "request", node="request_analyst", request=req.model_dump(mode="json"))
    update: dict[str, Any] = {
        "request": req,
        "assumptions": notes,
        "named_trail_ids": [t.trail_id for t in named],
        "clarification": None,
        "scope": "search",
        "excluded_trail_ids": list(previous.excluded_trail_ids) if previous else [],
    }
    if not req.region:
        question = _ask_for_region(he)
        update["clarification"] = question
        update["messages"] = [AIMessage(content=question)]
        return update

    scope, reason = decide_scope(previous, req, change)
    emit(runtime, "scope", node="request_analyst", scope=scope, reason=reason)
    update["scope"] = scope
    if previous is not None and previous.plan is not None:
        if change == "different_trail":
            update["excluded_trail_ids"] = [*update["excluded_trail_ids"], previous.plan.trail_id]
        elif scope == "replan":
            # Re-plan the trails agent 3 already approved; keep the same trail unless one is named.
            update |= {
                "candidates": previous.candidates,
                "assessments": previous.assessments,
                "evidence": previous.evidence,
                "named_trail_ids": update["named_trail_ids"] or [previous.plan.trail_id],
            }
    return update
