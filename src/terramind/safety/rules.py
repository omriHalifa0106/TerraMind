"""Deterministic safety rules. They run before the LLM critic and cannot be overridden by it.

A plan is vetoed if any issue is CRITICAL. The veto scope decides where the graph loops back:
"timing" (re-plan the same trail earlier) when every critical issue is a timing issue and an
earlier start is still possible, otherwise "trail" (search for another trail).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from terramind.schemas import (
    TIMING_CODES,
    IssueCode,
    RoutePlan,
    SafetyIssue,
    SafetyVerdict,
    Severity,
    TrailAlert,
    TrailCandidate,
    TrailStatus,
    TripRequest,
    WeatherSnapshot,
)


@dataclass(frozen=True)
class Thresholds:
    rain_prob_critical_water: int = 60  # % on water/canyon trails -> flood risk veto
    rain_prob_storm_warning: int = 60  # % elsewhere -> warning
    gust_warning_kmh: float = 50
    gust_critical_kmh: float = 75
    heat_warning_c: float = 32  # apparent temperature
    heat_critical_c: float = 37
    heat_hour: int = 11  # hiking past this local hour counts as "in the heat"
    sunset_buffer_min: int = 60  # finish at least this long before sunset
    earliest_start: time = time(6, 0)
    young_child_age: int = 8
    long_for_kids_km: float = 8


T = Thresholds()
WATER_TAGS = {"water", "canyon"}
CLOSING_KINDS = {"closure", "fire", "military", "maintenance"}


def _issue(trail: TrailCandidate, code: IssueCode, severity: Severity, evidence: str) -> SafetyIssue:
    return SafetyIssue(
        trail_id=trail.trail_id,
        trail_name=trail.name_he or trail.name,
        code=code,
        severity=severity,
        evidence=evidence,
        source="rule",
    )


def _end_dt(plan: RoutePlan, day: datetime) -> datetime:
    end = day.replace(hour=plan.end_time.hour, minute=plan.end_time.minute)
    start = day.replace(hour=plan.start_time.hour, minute=plan.start_time.minute)
    return end if end >= start else end + timedelta(days=1)


def evaluate(
    plan: RoutePlan,
    trail: TrailCandidate,
    request: TripRequest,
    weather: WeatherSnapshot | None,
    status: TrailStatus | None,
    alerts: list[TrailAlert],
) -> list[SafetyIssue]:
    tid = trail
    issues: list[SafetyIssue] = []
    is_water = bool(WATER_TAGS & set(trail.tags))

    # --- closures and alerts ---
    if status is not None and not status.is_open:
        issues.append(_issue(tid, "CLOSED", Severity.CRITICAL, f"trail status: closed ({status.reason})"))
    for a in alerts:
        if a.kind in CLOSING_KINDS and a.severity is Severity.CRITICAL:
            issues.append(_issue(tid, "CLOSED", Severity.CRITICAL, f"alert {a.alert_id}: {a.message}"))
        elif a.kind == "flood":
            # A flood warning endangers stream beds and canyons: veto water trails (or a trail the
            # alert names); for other trails in the region it is a warning, not a veto.
            sev = Severity.CRITICAL if is_water or a.trail_id == tid.trail_id else Severity.WARNING
            issues.append(_issue(tid, "FLOOD_RISK", sev, f"alert {a.alert_id}: {a.message}"))
    if status is not None and status.crowd_level == "high":
        issues.append(_issue(tid, "CROWDED", Severity.WARNING, "expected crowd level: high"))
    if status is not None and status.status == "unavailable":
        issues.append(_issue(tid, "DATA_UNAVAILABLE", Severity.WARNING, "trail status service unavailable"))

    # --- weather ---
    if weather is None or weather.status in ("unavailable", "out_of_range"):
        why = (
            "forecast beyond 16 days"
            if weather and weather.status == "out_of_range"
            else "forecast unavailable"
        )
        issues.append(_issue(tid, "DATA_UNAVAILABLE", Severity.WARNING, why))
    else:
        rain = weather.precip_prob_max_pct
        if rain is not None and is_water and rain >= T.rain_prob_critical_water:
            issues.append(
                _issue(
                    tid, "FLOOD_RISK", Severity.CRITICAL, f"rain probability {rain}% on a water/canyon trail"
                )
            )
        elif rain is not None and rain >= T.rain_prob_storm_warning:
            issues.append(_issue(tid, "STORM", Severity.WARNING, f"rain probability {rain}%"))
        gust = weather.wind_gust_max_kmh
        if gust is not None and gust >= T.gust_critical_kmh:
            issues.append(_issue(tid, "STORM", Severity.CRITICAL, f"wind gusts up to {gust:.0f} km/h"))
        elif gust is not None and gust >= T.gust_warning_kmh:
            issues.append(_issue(tid, "STORM", Severity.WARNING, f"wind gusts up to {gust:.0f} km/h"))
        heat = weather.apparent_temp_max_c
        if heat is not None and plan.end_time.hour >= T.heat_hour and not is_water:
            if heat >= T.heat_critical_c:
                issues.append(
                    _issue(
                        tid,
                        "HEAT",
                        Severity.CRITICAL,
                        f"apparent max {heat:.0f}°C, hiking until {plan.end_time:%H:%M}",
                    )
                )
            elif heat >= T.heat_warning_c:
                issues.append(_issue(tid, "HEAT", Severity.WARNING, f"apparent max {heat:.0f}°C"))
        if weather.sunset is not None:
            latest_end = weather.sunset - timedelta(minutes=T.sunset_buffer_min)
            if _end_dt(plan, weather.sunset.replace(hour=0, minute=0)) > latest_end:
                issues.append(
                    _issue(
                        tid,
                        "INSUFFICIENT_DAYLIGHT",
                        Severity.CRITICAL,
                        f"ends {plan.end_time:%H:%M}, sunset {weather.sunset:%H:%M}",
                    )
                )

    # --- group fit ---
    g = request.group
    if g.children and g.min_child_age is not None and g.min_child_age < T.young_child_age:
        if trail.difficulty == "hard":
            issues.append(
                _issue(
                    tid,
                    "DIFFICULTY_MISMATCH",
                    Severity.CRITICAL,
                    f"hard trail with a {g.min_child_age}-year-old",
                )
            )
        elif trail.length_km > T.long_for_kids_km:
            issues.append(
                _issue(
                    tid,
                    "DIFFICULTY_MISMATCH",
                    Severity.WARNING,
                    f"{trail.length_km:.1f} km with young children",
                )
            )
    return issues


def can_start_earlier(plan: RoutePlan, weather: WeatherSnapshot | None) -> bool:
    earliest = T.earliest_start
    if weather is not None and weather.sunrise is not None:
        earliest = max(earliest, (weather.sunrise + timedelta(minutes=15)).time())
    return plan.start_time > earliest


def verdict_from(
    issues: list[SafetyIssue], plan: RoutePlan, weather: WeatherSnapshot | None
) -> SafetyVerdict:
    critical = [i for i in issues if i.severity is Severity.CRITICAL]
    if not critical:
        return SafetyVerdict(approved=True, issues=issues)
    timing_only = all(i.code in TIMING_CODES for i in critical)
    if timing_only and can_start_earlier(plan, weather):
        return SafetyVerdict(
            approved=False,
            issues=issues,
            replan_scope="timing",
            guidance_for_replan="Start earlier: " + "; ".join(i.evidence for i in critical),
        )
    return SafetyVerdict(
        approved=False,
        issues=issues,
        rejected_trail_ids=[plan.trail_id],
        replan_scope="trail",
        guidance_for_replan="Choose another trail: " + "; ".join(i.evidence for i in critical),
    )
