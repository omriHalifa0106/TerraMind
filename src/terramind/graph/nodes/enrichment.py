"""Agent 3 - enrichment: real-time conditions and safety vetoes, BEFORE any planning.

For each of agent 2's best candidates, it asks the external services, all in parallel:
  - weather.get_forecast       (Open-Meteo: rain, wind, heat, sunrise/sunset for the trip date)
  - alerts.get_trail_status    (open/closed, expected crowding)
  - alerts.list_active_alerts  (fire, flood, military, maintenance alerts)
Then the deterministic rules in safety/rules.py approve or veto each candidate. No LLM is involved:
a veto is always reproducible and can always be traced to the data that caused it.
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

from langgraph.runtime import Runtime
from pydantic import ValidationError

from terramind.geo import DEFAULT_START, max_outing_minutes
from terramind.graph.nodes.common import emit
from terramind.graph.state import TerraMindContext, TerraMindState
from terramind.mcp_client.client import ToolGateway
from terramind.safety.rules import assess
from terramind.schemas import (
    CandidateAssessment,
    TrailAlert,
    TrailCandidate,
    TrailStatus,
    TripRequest,
    WeatherSnapshot,
)
from terramind.schemas.tool_results import AlertsResult, TrailStatusResult


async def _conditions(
    tools: ToolGateway, trail: TrailCandidate, req: TripRequest
) -> tuple[WeatherSnapshot | None, TrailStatus | None, list[TrailAlert]]:
    on = (req.trip_date or date.today()).isoformat()
    lat, lon = trail.trailhead
    # Closures and alerts are matched by name, and OSM keeps the route number and the Hebrew name
    # in different fields ("מסלול 2224" / "ליד נחל כזיב"), so check against all of them.
    names = " / ".join(dict.fromkeys(n for n in (trail.name, trail.name_he) if n))
    w_raw, s_raw, a_raw = await asyncio.gather(
        tools.call(
            "weather", "get_forecast", {"trail_id": trail.trail_id, "lat": lat, "lon": lon, "for_date": on}
        ),
        tools.call(
            "alerts", "get_trail_status", {"trail_id": trail.trail_id, "trail_name": names, "for_date": on}
        ),
        tools.call(
            "alerts",
            "list_active_alerts",
            {"region": req.region, "trail_id": trail.trail_id, "trail_name": names, "for_date": on},
        ),
    )
    weather: WeatherSnapshot | None
    try:
        weather = WeatherSnapshot.model_validate(w_raw)
    except ValidationError:
        weather = None  # the rules turn a missing forecast into a warning, never into "safe"
    status: TrailStatus | None
    try:
        status = TrailStatusResult.model_validate(s_raw).trail
    except ValidationError:
        status = None
    try:
        alerts = AlertsResult.model_validate(a_raw).alerts
    except ValidationError:
        alerts = []
    return weather, status, alerts


async def enrichment(state: TerraMindState, runtime: Runtime[TerraMindContext]) -> dict[str, Any]:
    ctx = runtime.context
    req = state["request"]
    to_check = state.get("candidates", [])[: ctx.settings.candidates_to_check]
    emit(runtime, "node", node="enrichment", status="start", candidates=len(to_check))

    conditions = await asyncio.gather(*(_conditions(ctx.tools, t, req) for t in to_check))
    start = req.start_time or DEFAULT_START
    assessments: list[CandidateAssessment] = []
    for trail, (weather, status, alerts) in zip(to_check, conditions, strict=True):
        duration = max_outing_minutes(trail.length_km, req.group.pace_kmh)
        a = assess(trail, req, weather, status, alerts, start, duration)
        assessments.append(a)
        emit(runtime, "assessment", node="enrichment", assessment=a.model_dump(mode="json"))
    return {"assessments": assessments}
