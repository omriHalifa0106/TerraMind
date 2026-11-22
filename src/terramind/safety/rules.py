"""Deterministic safety rules, used by agent 3 (enrichment) to veto unsafe candidate trails.

The LLM never decides safety: every veto comes from these rules, so the same data always gives
the same answer, and a user who writes "ignore the safety checks" cannot change it.

A candidate is checked BEFORE planning, against an estimated hiking window (start -> end).
A CRITICAL issue vetoes it. If the only critical issues are about timing (heat, daylight) and an
earlier start fixes them, the candidate is approved with that earlier start instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from terramind.schemas import (
    TIMING_CODES,
    CandidateAssessment,
    IssueCode,
    SafetyIssue,
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
    )


def evaluate(
    trail: TrailCandidate,
    request: TripRequest,
    weather: WeatherSnapshot | None,
    status: TrailStatus | None,
    alerts: list[TrailAlert],
    start: time,
    end: time,
) -> list[SafetyIssue]:
    """Every issue for one trail hiked from `start` to `end` on the requested day."""
    issues: list[SafetyIssue] = []
    is_water = bool(WATER_TAGS & set(trail.tags))

    # --- closures and alerts ---
    if status is not None and not status.is_open:
        why = f"המסלול סגור: {status.reason}" if status.reason else "המסלול סגור"
        issues.append(_issue(trail, "CLOSED", Severity.CRITICAL, why))
    for a in alerts:
        if a.kind in CLOSING_KINDS and a.severity is Severity.CRITICAL:
            issues.append(_issue(trail, "CLOSED", Severity.CRITICAL, f"התראה: {a.message}"))
        elif a.kind == "flood":
            # A flood warning endangers stream beds and canyons: veto water trails (or a trail the
            # alert names); for other trails in the region it is a warning, not a veto.
            sev = Severity.CRITICAL if is_water or a.trail_id == trail.trail_id else Severity.WARNING
            issues.append(_issue(trail, "FLOOD_RISK", sev, f"אזהרת שיטפונות: {a.message}"))
    if status is not None and status.crowd_level == "high":
        issues.append(_issue(trail, "CROWDED", Severity.WARNING, "צפוי עומס כבד"))
    if status is not None and status.status == "unavailable":
        issues.append(_issue(trail, "DATA_UNAVAILABLE", Severity.WARNING, "אין מידע עדכני על סגירות והתראות"))

    # --- weather ---
    if weather is None or weather.status in ("unavailable", "out_of_range"):
        why = (
            "אין תחזית: התאריך רחוק מ-16 ימים"
            if weather and weather.status == "out_of_range"
            else "אין תחזית מזג אוויר"
        )
        issues.append(_issue(trail, "DATA_UNAVAILABLE", Severity.WARNING, why))
    else:
        rain = weather.precip_prob_max_pct
        if rain is not None and is_water and rain >= T.rain_prob_critical_water:
            issues.append(
                _issue(
                    trail,
                    "FLOOD_RISK",
                    Severity.CRITICAL,
                    f"סיכוי לגשם {rain}% במסלול מים או ערוץ",
                )
            )
        elif rain is not None and rain >= T.rain_prob_storm_warning:
            issues.append(_issue(trail, "STORM", Severity.WARNING, f"סיכוי לגשם {rain}%"))
        gust = weather.wind_gust_max_kmh
        if gust is not None and gust >= T.gust_critical_kmh:
            issues.append(_issue(trail, "STORM", Severity.CRITICAL, f"משבי רוח עד {gust:.0f} קמ״ש"))
        elif gust is not None and gust >= T.gust_warning_kmh:
            issues.append(_issue(trail, "STORM", Severity.WARNING, f"משבי רוח עד {gust:.0f} קמ״ש"))
        heat = weather.apparent_temp_max_c
        if heat is not None and end.hour >= T.heat_hour and not is_water:
            if heat >= T.heat_critical_c:
                issues.append(
                    _issue(
                        trail,
                        "HEAT",
                        Severity.CRITICAL,
                        f"עומס חום של {heat:.0f}°C, והליכה עד {end:%H:%M}",
                    )
                )
            elif heat >= T.heat_warning_c:
                issues.append(_issue(trail, "HEAT", Severity.WARNING, f"עומס חום של {heat:.0f}°C"))
        if weather.sunset is not None:
            latest_end = (weather.sunset - timedelta(minutes=T.sunset_buffer_min)).time()
            if end < start:  # the outing would run past midnight: not a day hike at all
                why = (
                    f"ארוך מדי ליום אחד ({trail.length_km:.0f} ק״מ): "
                    f"גם ביציאה ב-{start:%H:%M} לא מסיימים באותו יום"
                )
                issues.append(_issue(trail, "INSUFFICIENT_DAYLIGHT", Severity.CRITICAL, why))
            elif end > latest_end:
                why = f"הסיום ב-{end:%H:%M}, קרוב מדי לשקיעה ב-{weather.sunset:%H:%M}"
                issues.append(_issue(trail, "INSUFFICIENT_DAYLIGHT", Severity.CRITICAL, why))

    # --- group fit ---
    g = request.group
    if g.children and g.min_child_age is not None and g.min_child_age < T.young_child_age:
        if trail.difficulty == "hard":
            issues.append(
                _issue(
                    trail,
                    "DIFFICULTY_MISMATCH",
                    Severity.CRITICAL,
                    f"מסלול קשה לילד בן {g.min_child_age}",
                )
            )
        elif trail.length_km > T.long_for_kids_km:
            issues.append(
                _issue(
                    trail,
                    "DIFFICULTY_MISMATCH",
                    Severity.WARNING,
                    f"{trail.length_km:.1f} ק״מ עם ילדים קטנים",
                )
            )
    return issues


def earliest_start(weather: WeatherSnapshot | None) -> time:
    """Never before 06:00, and not before 15 minutes after sunrise."""
    earliest = T.earliest_start
    if weather is not None and weather.sunrise is not None:
        earliest = max(earliest, (weather.sunrise + timedelta(minutes=15)).time())
    return earliest


def _plus(t: time, minutes: int) -> time:
    return (datetime.combine(date.min, t) + timedelta(minutes=minutes)).time()


def assess(
    trail: TrailCandidate,
    request: TripRequest,
    weather: WeatherSnapshot | None,
    status: TrailStatus | None,
    alerts: list[TrailAlert],
    start: time,
    duration_minutes: int,
) -> CandidateAssessment:
    """Approve or veto one candidate. A timing-only veto is retried once at the earliest safe start."""

    def check(at: time) -> list[SafetyIssue]:
        return evaluate(trail, request, weather, status, alerts, at, _plus(at, duration_minutes))

    def result(approved: bool, at: time, issues: list[SafetyIssue]) -> CandidateAssessment:
        return CandidateAssessment(
            trail_id=trail.trail_id,
            trail_name=trail.name_he or trail.name,
            approved=approved,
            start_time=at,
            issues=issues,
            weather=weather,
            trail_status=status,
            alerts=alerts,
        )

    issues = check(start)
    critical = [i for i in issues if i.severity is Severity.CRITICAL]
    if not critical:
        return result(True, start, issues)
    earlier = earliest_start(weather)
    if all(i.code in TIMING_CODES for i in critical) and earlier < start:
        retry = check(earlier)
        if not any(i.severity is Severity.CRITICAL for i in retry):
            # Keep the reason visible: the presenter tells the user why the start moved.
            moved = [
                _issue(trail, i.code, Severity.INFO, f"היציאה הוקדמה ל-{earlier:%H:%M}: {i.evidence}")
                for i in critical
            ]
            return result(True, earlier, [*moved, *retry])
    return result(False, start, issues)
