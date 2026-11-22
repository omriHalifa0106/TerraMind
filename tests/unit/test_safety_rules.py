"""The deterministic safety rules agent 3 uses to approve or veto a candidate trail."""

from __future__ import annotations

from datetime import UTC, date, datetime, time

from terramind.safety.rules import assess, evaluate
from terramind.schemas import (
    Difficulty,
    GroupProfile,
    Severity,
    TrailAlert,
    TrailCandidate,
    TrailStatus,
    TripRequest,
    WeatherSnapshot,
)

DAY = date(2026, 12, 5)


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


def ev(
    t: TrailCandidate,
    req: TripRequest,
    w: WeatherSnapshot | None,
    status: TrailStatus | None,
    alerts: list[TrailAlert],
    start: time = time(8),
    end: time = time(11),
) -> list:  # type: ignore[type-arg]
    return evaluate(t, req, w, status, alerts, start, end)


def test_clean_trail_is_approved() -> None:
    assert ev(trail(), REQ, weather(), OPEN, []) == []
    a = assess(trail(), REQ, weather(), OPEN, [], time(8), 180)
    assert a.approved and a.start_time == time(8)


def test_closed_trail_is_vetoed() -> None:
    closed = TrailStatus(trail_id="t1", is_open=False, reason="maintenance")
    a = assess(trail(), REQ, weather(), closed, [], time(8), 180)
    assert not a.approved and ("CLOSED", "critical") in codes(a.issues)


def test_rain_on_water_trail_is_flood_risk_but_only_storm_warning_elsewhere() -> None:
    wet = weather(precip_prob_max_pct=70)
    assert ("FLOOD_RISK", "critical") in codes(ev(trail(["water"]), REQ, wet, OPEN, []))
    dry_trail = codes(ev(trail(["viewpoint"]), REQ, wet, OPEN, []))
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
    assert ("FLOOD_RISK", "critical") in codes(ev(trail(["water"]), REQ, weather(), OPEN, [alert]))
    assert ("FLOOD_RISK", "warning") in codes(ev(trail(), REQ, weather(), OPEN, [alert]))


def test_late_finish_is_fixed_by_an_earlier_start() -> None:
    # 13:00 + 190 min = 16:10, but sunset is 16:40 and the rules want a 60-minute buffer.
    assert ("INSUFFICIENT_DAYLIGHT", "critical") in codes(
        ev(trail(), REQ, weather(), OPEN, [], time(13), time(16, 10))
    )
    a = assess(trail(), REQ, weather(), OPEN, [], time(13), 190)
    assert a.approved and a.start_time == time(6, 35)  # sunrise 06:20 + 15 minutes
    assert ("INSUFFICIENT_DAYLIGHT", "info") in codes(a.issues)  # the reason stays visible


def test_timing_veto_stands_when_no_earlier_start_helps() -> None:
    a = assess(trail(), REQ, weather(), OPEN, [], time(6, 30), 600)  # 10 hours never fit before sunset
    assert not a.approved and ("INSUFFICIENT_DAYLIGHT", "critical") in codes(a.issues)


def test_unavailable_weather_is_a_warning_not_a_pass_or_a_veto() -> None:
    w = WeatherSnapshot.unavailable("t1", DAY, status="unavailable")
    a = assess(trail(), REQ, w, OPEN, [], time(8), 180)
    assert a.approved and codes(a.issues) == {("DATA_UNAVAILABLE", "warning")}


def test_hard_trail_with_young_children_is_vetoed() -> None:
    req = REQ.model_copy(update={"group": GroupProfile(children=2, min_child_age=5)})
    assert ("DIFFICULTY_MISMATCH", "critical") in codes(
        ev(trail(difficulty=Difficulty.HARD), req, weather(), OPEN, [])
    )


def test_heat_warning_on_exposed_trail_only() -> None:
    hot = weather(apparent_temp_max_c=34)
    assert ("HEAT", "warning") in codes(ev(trail(), REQ, hot, OPEN, [], end=time(13)))
    assert not any(c == "HEAT" for c, _ in codes(ev(trail(["water"]), REQ, hot, OPEN, [], end=time(13))))


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
    assert ("FLOOD_RISK", "critical") in codes(ev(trail(["water"]), REQ, weather(), OPEN, [alert]))
    dry = codes(ev(trail(["viewpoint"]), REQ, weather(), OPEN, [alert]))
    assert ("FLOOD_RISK", "warning") in dry and ("FLOOD_RISK", "critical") not in dry
    named = alert.model_copy(update={"trail_id": "t1"})
    assert ("FLOOD_RISK", "critical") in codes(ev(trail(["viewpoint"]), REQ, weather(), OPEN, [named]))
