from __future__ import annotations

from datetime import UTC, date, datetime, time

from terramind.safety.rules import evaluate, verdict_from
from terramind.schemas import (
    Difficulty,
    GroupProfile,
    RoutePlan,
    Severity,
    Stop,
    TrailAlert,
    TrailCandidate,
    TrailStatus,
    TripRequest,
    WeatherSnapshot,
)

DAY = date(2026, 12, 5)


def plan(start: time = time(8), end: time = time(11)) -> RoutePlan:
    stops = [
        Stop(order=0, name="a", kind="trailhead", location=(33, 35)),
        Stop(order=1, name="b", kind="end", location=(33, 35)),
    ]
    return RoutePlan(
        plan_id="p",
        trail_id="t1",
        title="x",
        stops=stops,
        walk_km=6,
        walk_minutes=150,
        start_time=start,
        end_time=end,
    )


def trail(
    tags: list[str] | None = None, difficulty: Difficulty = Difficulty.MODERATE, km: float = 6
) -> TrailCandidate:
    return TrailCandidate(
        trail_id="t1",
        name="Nahal Test",
        region="r",
        length_km=km,
        difficulty=difficulty,
        tags=tags or [],
        trailhead=(33.0, 35.3),
    )


def weather(**kw: object) -> WeatherSnapshot:
    base: dict[str, object] = {
        "trail_id": "t1",
        "for_date": DAY,
        "precip_prob_max_pct": 5,
        "wind_gust_max_kmh": 20,
        "apparent_temp_max_c": 22,
        "sunrise": datetime(2026, 12, 5, 6, 20),
        "sunset": datetime(2026, 12, 5, 16, 40),
    }
    return WeatherSnapshot.model_validate({**base, **kw})


REQ = TripRequest(region="Upper Galilee", trip_date=DAY, semantic_query="q")
OPEN = TrailStatus(trail_id="t1", is_open=True, crowd_level="low")


def codes(issues: list) -> set[tuple[str, str]]:  # type: ignore[type-arg]
    return {(i.code, i.severity.value) for i in issues}


def test_clean_plan_is_approved() -> None:
    issues = evaluate(plan(), trail(), REQ, weather(), OPEN, [])
    assert issues == []
    assert verdict_from(issues, plan(), weather()).approved


def test_closed_trail_is_vetoed_at_trail_scope() -> None:
    closed = TrailStatus(trail_id="t1", is_open=False, reason="maintenance")
    issues = evaluate(plan(), trail(), REQ, weather(), closed, [])
    v = verdict_from(issues, plan(), weather())
    assert not v.approved and v.replan_scope == "trail" and v.rejected_trail_ids == ["t1"]


def test_rain_on_water_trail_is_flood_risk_but_only_storm_warning_elsewhere() -> None:
    wet = weather(precip_prob_max_pct=70)
    assert ("FLOOD_RISK", "critical") in codes(evaluate(plan(), trail(["water"]), REQ, wet, OPEN, []))
    dry_trail = codes(evaluate(plan(), trail(["viewpoint"]), REQ, wet, OPEN, []))
    assert ("STORM", "warning") in dry_trail and ("FLOOD_RISK", "critical") not in dry_trail


def test_flood_alert_is_critical_for_water_trails() -> None:
    alert = TrailAlert(
        alert_id="a",
        trail_id=None,
        region="golan",
        kind="flood",
        severity=Severity.WARNING,
        message="flood",
        valid_from=datetime(2026, 12, 5, tzinfo=UTC),
        valid_to=None,
    )
    assert ("FLOOD_RISK", "critical") in codes(
        evaluate(plan(), trail(["water"]), REQ, weather(), OPEN, [alert])
    )
    assert ("FLOOD_RISK", "warning") in codes(evaluate(plan(), trail(), REQ, weather(), OPEN, [alert]))


def test_late_finish_is_a_timing_veto_that_can_be_fixed_earlier() -> None:
    late = plan(start=time(13), end=time(16, 10))
    issues = evaluate(late, trail(), REQ, weather(), OPEN, [])
    assert ("INSUFFICIENT_DAYLIGHT", "critical") in codes(issues)
    v = verdict_from(issues, late, weather())
    assert not v.approved and v.replan_scope == "timing" and v.rejected_trail_ids == []


def test_timing_veto_becomes_trail_veto_when_no_earlier_start_exists() -> None:
    dawn = plan(start=time(6, 30), end=time(16, 30))  # sunrise 06:20 + 15 min = 06:35 > 06:30
    v = verdict_from(evaluate(dawn, trail(), REQ, weather(), OPEN, []), dawn, weather())
    assert v.replan_scope == "trail"


def test_unavailable_weather_is_a_warning_not_a_pass_or_a_veto() -> None:
    w = WeatherSnapshot.unavailable("t1", DAY, status="unavailable")
    issues = evaluate(plan(), trail(), REQ, w, OPEN, [])
    assert codes(issues) == {("DATA_UNAVAILABLE", "warning")}
    assert verdict_from(issues, plan(), w).approved


def test_hard_trail_with_young_children_is_vetoed() -> None:
    req = REQ.model_copy(update={"group": GroupProfile(children=2, min_child_age=5)})
    issues = evaluate(plan(), trail(difficulty=Difficulty.HARD), req, weather(), OPEN, [])
    assert ("DIFFICULTY_MISMATCH", "critical") in codes(issues)


def test_heat_warning_on_exposed_trail_only() -> None:
    hot = weather(apparent_temp_max_c=34)
    assert ("HEAT", "warning") in codes(evaluate(plan(end=time(13)), trail(), REQ, hot, OPEN, []))
    assert not any(
        c == "HEAT" for c, _ in codes(evaluate(plan(end=time(13)), trail(["water"]), REQ, hot, OPEN, []))
    )


def test_regional_flood_warning_vetoes_only_water_trails() -> None:
    alert = TrailAlert(
        alert_id="a",
        trail_id=None,
        region="golan",
        kind="flood",
        severity=Severity.CRITICAL,
        message="flood",
        valid_from=datetime(2026, 12, 5, tzinfo=UTC),
        valid_to=None,
    )
    assert ("FLOOD_RISK", "critical") in codes(
        evaluate(plan(), trail(["water"]), REQ, weather(), OPEN, [alert])
    )
    dry = codes(evaluate(plan(), trail(["viewpoint"]), REQ, weather(), OPEN, [alert]))
    assert ("FLOOD_RISK", "warning") in dry and ("FLOOD_RISK", "critical") not in dry
    named = alert.model_copy(update={"trail_id": "t1"})
    assert ("FLOOD_RISK", "critical") in codes(
        evaluate(plan(), trail(["viewpoint"]), REQ, weather(), OPEN, [named])
    )
