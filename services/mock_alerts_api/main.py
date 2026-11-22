"""Mock Alerts API: simulates trail closures, safety alerts and crowding for edge-case testing.

The active scenario is switched only through the admin route `POST /scenarios/{name}`; the
alerts MCP server exposes read-only tools, so an agent can never "reopen" a closed trail.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from terramind.regions import resolve_region

SCENARIO_DIR = Path(__file__).resolve().parent / "scenarios"


class ClosureRule(BaseModel):
    match: list[str]  # substrings of the trail name / id; "*" matches every trail
    reason: str


class AlertRule(BaseModel):
    alert_id: str
    region: str | None = None  # region key; None = all regions
    trail_match: list[str] = Field(default_factory=list)
    kind: Literal["closure", "flood", "fire", "crowding", "military", "maintenance"]
    severity: Literal["info", "warning", "critical"]
    message: str


class CrowdRule(BaseModel):
    match: list[str]
    level: Literal["low", "medium", "high"]


class Scenario(BaseModel):
    name: str
    description: str
    closures: list[ClosureRule] = Field(default_factory=list)
    alerts: list[AlertRule] = Field(default_factory=list)
    crowding: list[CrowdRule] = Field(default_factory=list)


def load_scenarios() -> dict[str, Scenario]:
    return {
        p.stem: Scenario.model_validate(json.loads(p.read_text(encoding="utf-8")))
        for p in sorted(SCENARIO_DIR.glob("*.json"))
    }


SCENARIOS = load_scenarios()
app = FastAPI(title="TerraMind Mock Alerts API", version="0.1.0")
app.state.current = "normal"


def _current() -> Scenario:
    return SCENARIOS[app.state.current]


def _matches(patterns: list[str], trail_id: str, name: str | None) -> bool:
    haystack = f"{trail_id} {name or ''}".lower()
    return any(p == "*" or p.lower() in haystack for p in patterns)


def _alert_payload(rule: AlertRule, on: date, trail_id: str | None) -> dict[str, Any]:
    start = datetime.combine(on, time.min, tzinfo=UTC)
    return {
        "alert_id": rule.alert_id,
        "trail_id": trail_id,
        "region": rule.region,
        "kind": rule.kind,
        "severity": rule.severity,
        "message": rule.message,
        "valid_from": start.isoformat(),
        "valid_to": (start + timedelta(days=7)).isoformat(),
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "scenario": app.state.current}


@app.get("/scenarios")
async def list_scenarios() -> dict[str, str]:
    return {name: s.description for name, s in SCENARIOS.items()}


@app.get("/scenarios/current")
async def current_scenario() -> Scenario:
    return _current()


@app.post("/scenarios/{name}")
async def set_scenario(name: str) -> dict[str, str]:
    if name not in SCENARIOS:
        raise HTTPException(404, f"Unknown scenario {name!r}. Known: {sorted(SCENARIOS)}")
    app.state.current = name
    return {"scenario": name}


@app.get("/trails/status")
async def trail_status(trail_id: str, name: str | None = None, on: date | None = None) -> dict[str, Any]:
    sc = _current()
    closure = next((c for c in sc.closures if _matches(c.match, trail_id, name)), None)
    crowd = next((c.level for c in sc.crowding if _matches(c.match, trail_id, name)), "low")
    return {
        "trail_id": trail_id,
        "is_open": closure is None,
        "reason": closure.reason if closure else None,
        "crowd_level": crowd,
    }


@app.get("/alerts")
async def alerts(
    region: str | None = None,
    trail_id: str | None = None,
    name: str | None = None,
    on: date | None = None,
) -> list[dict[str, Any]]:
    on = on or date.today()
    wanted = resolve_region(region)
    out: list[dict[str, Any]] = []
    for rule in _current().alerts:
        region_ok = rule.region is None or wanted is None or rule.region == wanted.key
        trail_ok = not rule.trail_match or (
            trail_id is not None and _matches(rule.trail_match, trail_id, name)
        )
        if region_ok and trail_ok:
            out.append(_alert_payload(rule, on, trail_id if rule.trail_match else None))
    return out
