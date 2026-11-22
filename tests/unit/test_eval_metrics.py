"""Eval metrics are pure functions over a run output dict: no DB, no LLM."""

from __future__ import annotations

from typing import Any

from evals.evaluators.metrics import intake_accuracy, safety_loop


def _output(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "plan": {"trail_id": "t1"},
        "plan_trail_name": "Nahal Amud",
        "plan_difficulty": "easy",
        "plan_length_km": 4.2,
        "attempts": 1,
        "vetoes": 0,
        "path": ["intake", "planner"],
        "data": {"request": None, "verdicts": []},
    }
    base.update(overrides)
    return base


def test_max_vetoes_fails_when_soft_issue_became_veto() -> None:
    assert safety_loop(_output(vetoes=1), {"max_vetoes": 0})["safety_loop"] < 1.0
    assert safety_loop(_output(vetoes=0), {"max_vetoes": 0})["safety_loop"] == 1.0


def test_max_plan_length() -> None:
    assert safety_loop(_output(plan_length_km=7.5), {"max_plan_length_km": 5})["safety_loop"] < 1.0
    assert safety_loop(_output(plan_length_km=4.2), {"max_plan_length_km": 5})["safety_loop"] == 1.0


def test_max_plan_length_skipped_without_plan() -> None:
    out = _output(plan=None, plan_length_km=None)
    assert safety_loop(out, {"max_plan_length_km": 5})["safety_loop"] == 1.0


def test_expect_issue_codes() -> None:
    verdicts = [{"approved": True, "issues": [{"code": "DATA_UNAVAILABLE"}]}]
    case = {"expect_issue_codes": ["DATA_UNAVAILABLE"]}
    assert safety_loop(_output(data={"verdicts": verdicts}), case)["safety_loop"] == 1.0
    assert safety_loop(_output(), case)["safety_loop"] < 1.0


def test_intake_accuracy_subset_match() -> None:
    request = {
        "difficulty": "easy",
        "trail_types": ["water", "family"],
        "group": {"adults": 2, "children": 2, "min_child_age": 2},
    }
    out = _output(data={"request": request, "verdicts": []})
    case = {
        "expected_request": {"trail_types": ["water"], "group": {"min_child_age": 2}, "difficulty": "hard"}
    }
    assert intake_accuracy(out, case) == {"intake_accuracy": 0.667}


def test_intake_accuracy_absent_when_not_defined() -> None:
    assert intake_accuracy(_output(), {}) == {}


def test_intake_accuracy_zero_without_request() -> None:
    case = {"expected_request": {"difficulty": "easy"}}
    assert intake_accuracy(_output(), case) == {"intake_accuracy": 0.0}
