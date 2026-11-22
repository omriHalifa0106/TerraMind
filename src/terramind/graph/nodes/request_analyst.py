"""Agent 1 - request analysis.

Takes what the user wrote ("בא לי מסלול מים בצפון") and turns it into a structured TripRequest:
region, date, difficulty, trail types, group. Claude (Haiku) does the extraction; code then fills
the gaps it can verify (the region of a trail the user named, tomorrow as the default date).
If no region can be found, the agent asks the user instead of guessing, and the run ends.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from langchain_core.messages import AIMessage, SystemMessage
from langgraph.runtime import Runtime

from terramind.graph.nodes.common import emit, error, is_hebrew, last_user_text, prompt
from terramind.graph.state import TerraMindContext, TerraMindState
from terramind.llm.caching import cached_system
from terramind.llm.client import LLMError
from terramind.regions import region_of, resolve_region
from terramind.schemas import TrailCandidate, TripRequest
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

    try:
        req = await runtime.context.llm.structured(
            "request_analyst",
            TripRequest,
            [
                cached_system(prompt("request_analyst")),
                SystemMessage(content=f"TODAY: {today.isoformat()} ({today:%A})"),
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

    notes: list[str] = []
    named = await _named_trails(runtime, text)
    if not req.region and (found := resolve_region(text)) is not None:
        req = req.model_copy(update={"region": found.name_en})
    if not req.region and named and (found := region_of(named[0].trailhead)) is not None:
        req = req.model_copy(update={"region": found.name_en})
        notes.append(f"Region inferred from the trail you named ({named[0].name_he or named[0].name}).")
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
    }
    if not req.region:
        question = _ask_for_region(he)
        update["clarification"] = question
        update["messages"] = [AIMessage(content=question)]
    return update
