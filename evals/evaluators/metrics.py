"""Deterministic evaluators. Each takes the run output and the case definition, returns 0..1."""

from __future__ import annotations

import json
import re
from typing import Any

NUMBER = re.compile(r"\d+(?:[.:]\d+)?")


# Lookups any turn may make (e.g. inferring the region from a named trail) are never penalized.
LOOKUP_TOOLS = {"gis.find_trails_by_name"}
PLANNING_TOOLS = {
    "gis.search_trails",
    "gis.get_trail_details",
    "gis.find_pois_near_trail",
    "gis.estimate_leg",
    "knowledge.hybrid_search",
    "weather.get_forecast",
    "alerts.get_trail_status",
    "alerts.list_active_alerts",
}


def tool_accuracy(output: dict[str, Any], case: dict[str, Any]) -> dict[str, float]:
    # A call that timed out or failed did not do its job: it does not count toward recall.
    called = {
        f"{c['server']}.{c['tool']}" for c in output["tool_calls"] if c.get("status") in ("ok", "empty")
    } - LOOKUP_TOOLS
    expected = set(case.get("expected_tools", []))
    if not expected:  # e.g. smalltalk / clarification: no planning tool should run at all
        ok = 0.0 if called & PLANNING_TOOLS else 1.0
        return {"tool_recall": ok, "tool_precision": ok}
    recall = len(called & expected) / len(expected)
    precision = len(called & PLANNING_TOOLS) / len(called) if called else 0.0
    return {"tool_recall": round(recall, 3), "tool_precision": round(precision, 3)}


def safety_loop(output: dict[str, Any], case: dict[str, Any]) -> dict[str, float]:
    checks: list[bool] = []
    name = (output.get("plan_trail_name") or "").lower()
    for pattern in case.get("forbidden_trail_patterns", []):
        checks.append(pattern.lower() not in name)
    if "expect_plan" in case:
        checks.append((output.get("plan") is not None) == case["expect_plan"])
    if case.get("expect_veto"):
        checks.append(output["vetoes"] >= 1)
    if case.get("forbidden_difficulty"):
        checks.append(output.get("plan_difficulty") != case["forbidden_difficulty"])
    if case.get("expected_route"):
        checks.append(case["expected_route"] in output["path"])
    if "max_vetoes" in case:  # soft issues (e.g. crowding) must warn, not veto
        checks.append(output["vetoes"] <= case["max_vetoes"])
    if "max_plan_length_km" in case and output.get("plan") is not None:
        length = output.get("plan_length_km")
        checks.append(length is not None and length <= case["max_plan_length_km"])
    if case.get("expect_issue_codes"):  # e.g. DATA_UNAVAILABLE must surface, not pass silently
        raised = {
            issue["code"]
            for verdict in output.get("data", {}).get("verdicts", [])
            for issue in verdict.get("issues", [])
        }
        checks.extend(code in raised for code in case["expect_issue_codes"])
    checks.append(output["attempts"] <= 3)
    return {"safety_loop": round(sum(checks) / len(checks), 3)}


def _matches(actual: Any, expected: Any) -> bool:
    """Subset match: dicts recurse, an expected list must be contained in the actual list."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(_matches(actual.get(k), v) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and all(item in actual for item in expected)
    return bool(actual == expected)


def intake_accuracy(output: dict[str, Any], case: dict[str, Any]) -> dict[str, float]:
    """Share of `expected_request` fields that intake extracted correctly (only when defined)."""
    expected: dict[str, Any] = case.get("expected_request") or {}
    if not expected:
        return {}
    request = output.get("data", {}).get("request") or {}
    hits = sum(_matches(request.get(k), v) for k, v in expected.items())
    return {"intake_accuracy": round(hits / len(expected), 3)}


def grounding(output: dict[str, Any], case: dict[str, Any]) -> dict[str, float]:
    """Proxy for faithfulness: citations must exist, numbers in the answer must appear in the data."""
    summary = output.get("summary") or ""
    if not summary:
        return {"citation_validity": 1.0, "number_grounding": 1.0}
    evidence_ids = set(output.get("evidence_ids", []))
    cited = re.findall(r"\[(kc-[0-9a-f]{16})\]", summary)
    citation_validity = sum(c in evidence_ids for c in cited) / len(cited) if cited else 1.0
    data_blob = json.dumps(output.get("data", {}), ensure_ascii=False)
    text_wo_ids = re.sub(r"\[kc-[0-9a-f]{16}\]", "", summary)
    numbers = [n for n in NUMBER.findall(text_wo_ids) if len(n) > 1 or n not in "0123456789"]
    grounded = sum(n in data_blob for n in numbers) / len(numbers) if numbers else 1.0
    return {"citation_validity": round(citation_validity, 3), "number_grounding": round(grounded, 3)}


ALL = (tool_accuracy, safety_loop, grounding, intake_accuracy)
