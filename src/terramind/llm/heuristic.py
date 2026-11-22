"""Deterministic, offline stand-in for the LLM (LLM_BACKEND=heuristic).

It implements the same LLMClient protocol with keyword rules, so the whole graph, the API and
the evals can run without an API key or network. It is NOT a quality baseline: use it to test
wiring, loops and safety behaviour, and to demo the system offline.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Sequence
from datetime import date, timedelta
from typing import Any, cast

from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from terramind.llm.router import Task
from terramind.regions import resolve_region
from terramind.schemas import (
    Difficulty,
    GradingResult,
    GroupProfile,
    LlmSafetyReview,
    RelevanceGrade,
    TrailType,
    TripRequest,
)

TYPE_WORDS: dict[TrailType, tuple[str, ...]] = {
    TrailType.WATER: ("water", "stream", "spring", "waterfall", "מים", "נחל", "מעיין", "מפל"),
    TrailType.VIEWPOINT: ("view", "viewpoint", "תצפית", "נוף"),
    TrailType.FOREST: ("forest", "woods", "יער", "חורש"),
    TrailType.HISTORY: ("history", "castle", "ruins", "היסטור", "עתיק", "מבצר", "מצודה"),
    TrailType.LOOP: ("loop", "circular", "מעגלי"),
    TrailType.FAMILY: ("family", "kids", "children", "משפח", "ילדים", "ילד"),
}
REGION_WORDS = ("golan", "גולן", "meron", "מירון", "galilee", "גליל", "north", "צפון")
GREETINGS = ("hi", "hello", "thanks", "thank you", "שלום", "היי", "תודה")
WEEKDAYS = {"saturday": 5, "שבת": 5, "friday": 4, "שישי": 4, "sunday": 6, "ראשון": 6}


def _last_human(messages: Sequence[BaseMessage]) -> str:
    for m in reversed(messages):
        if isinstance(m, HumanMessage) and isinstance(m.content, str):
            return m.content
    return ""


def _field(messages: Sequence[BaseMessage], label: str) -> Any:
    """Read a 'LABEL: <json>' line that the nodes put into their prompts."""
    for m in messages:
        content = m.content if isinstance(m.content, str) else ""
        match = re.search(rf"^{label}: (.*)$", content, flags=re.M)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                return match.group(1)
    return None


def extract_request(text: str, today: date) -> TripRequest:
    low = text.lower()
    region = next((w for w in REGION_WORDS if w in low), None)
    resolved = resolve_region(region) if region else None
    types = [t for t, words in TYPE_WORDS.items() if any(w in low for w in words)]
    difficulty = None
    if any(w in low for w in ("easy", "קל", "קליל")):
        difficulty = Difficulty.EASY
    elif any(w in low for w in ("hard", "challenging", "קשה", "מאתגר")):
        difficulty = Difficulty.HARD
    trip_date = None
    if any(w in low for w in ("tomorrow", "מחר")):
        trip_date = today + timedelta(days=1)
    for word, weekday in WEEKDAYS.items():
        if word in low:
            trip_date = today + timedelta(days=(weekday - today.weekday()) % 7 or 7)
    kids = re.search(r"(\d+)\s*(?:kids|children|ילדים)", low)
    age = re.search(r"(?:age[sd]?|בני|בגיל)\s*(\d{1,2})", low)
    group = GroupProfile(
        children=int(kids.group(1)) if kids else (1 if TrailType.FAMILY in types else 0),
        min_child_age=int(age.group(1)) if age else None,
    )
    km = re.search(r"(\d+(?:\.\d+)?)\s*(?:km|ק\"מ|קמ)", low)
    return TripRequest(
        region=resolved.name_en if resolved else None,
        trip_date=trip_date,
        difficulty=difficulty,
        trail_types=types,
        max_length_km=float(km.group(1)) if km else None,
        group=group,
        semantic_query=text.strip()[:200],
        missing_fields=[] if resolved else ["region"],
    )


class HeuristicLLM:
    async def structured[T: BaseModel](
        self,
        task: Task,
        schema: type[T],
        messages: Sequence[BaseMessage],
        config: RunnableConfig | None = None,
    ) -> T:
        text = _last_human(messages)
        low = text.lower()
        if task == "supervisor":
            has_plan = any("EXISTING PLAN: yes" in str(m.content) for m in messages)
            greeting = any(re.search(rf"(?<!\w){re.escape(g)}(?!\w)", low) for g in GREETINGS)
            if len(low) < 25 and greeting and not any(r in low for r in REGION_WORDS):
                intent = "smalltalk"
            elif has_plan and ("?" in text or any(w in low for w in ("why", "למה", "מדוע"))):
                intent = "question"
            elif has_plan and any(w in low for w in ("shorter", "longer", "instead", "קצר", "ארוך", "במקום")):
                intent = "refine_plan"
            else:
                intent = "new_plan"
            return schema.model_validate({"intent": intent, "reason": "keyword rules"})
        if task == "intake":
            today_raw = _field(messages, "TODAY")
            today = date.fromisoformat(str(today_raw).split()[0]) if today_raw else date.today()
            return cast(T, extract_request(text, today))
        if task == "grader":
            chunks = _field(messages, "CHUNKS") or []
            return cast(
                T,
                GradingResult(
                    grades=[
                        RelevanceGrade(chunk_id=c["chunk_id"], relevant=True, reason="heuristic: keep all")
                        for c in chunks
                    ]
                ),
            )
        if task == "safety_critic":
            return cast(T, LlmSafetyReview())
        if task == "planner":
            candidates = _field(messages, "CANDIDATES") or []
            first = candidates[0] if candidates else {"trail_id": "", "name": "", "name_he": None}
            return schema.model_validate(
                {
                    "trail_id": first["trail_id"],
                    "title": first.get("name_he") or first["name"],
                    "poi_ids": [],
                    "rationale": "Top-ranked candidate by spatial + semantic fusion.",
                }
            )
        raise NotImplementedError(f"HeuristicLLM has no rule for task {task!r}")

    async def agent_structured[T: BaseModel](
        self,
        task: Task,
        schema: type[T],
        messages: Sequence[BaseMessage],
        tools: Sequence[BaseTool],
        config: RunnableConfig | None = None,
    ) -> tuple[T, list[dict[str, Any]]]:
        return await self.structured(task, schema, messages, config), []

    async def stream_text(
        self, task: Task, messages: Sequence[BaseMessage], config: RunnableConfig | None = None
    ) -> AsyncIterator[str]:
        data = _field(messages, "DATA") or {}
        text = _render(data, _field(messages, "USER MESSAGE") or _last_human(messages))
        for word in re.split(r"(\s+)", text):
            yield word


def _render(data: dict[str, Any], user_text: str) -> str:
    he = bool(re.search(r"[֐-׿]", str(user_text)))
    plan = data.get("plan")
    lines: list[str] = []
    if not plan:
        lines.append("**לא נמצא מסלול בטוח.**" if he else "**No safe route found.**")
        lines += [f"- {n}" for n in data.get("safety_notes", [])]
        return "\n".join(lines)
    lines.append(
        f"**{plan['title']}** · {plan['start_time'][:5]}-{plan['end_time'][:5]} · {plan['walk_km']} km"
    )
    for s in plan["stops"]:
        lines.append(f"{s['order'] + 1}. {s['name']} ({(s.get('arrive_at') or '')[:5]})")
    for t in data.get("tradeoffs", []):
        name = t.get("rejected_trail_name") or t["rejected_trail_id"]
        lines.append(f"- {'לא' if he else 'Not'} {name}: {t['detail']}")
    for n in data.get("safety_notes", []):
        lines.append(f"- {n}")
    for ev in data.get("evidence", [])[:2]:
        lines.append(f"- {ev['text'][:140]} [{ev['id']}]")
    return "\n".join(lines)
