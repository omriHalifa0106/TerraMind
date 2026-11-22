"""Which agents a follow-up message needs: decided by code from what changed, not by the model.

A change to what is searched or checked for safety (area, date, start time, difficulty, type, length,
group) searches and checks again: a plan is never built on safety checks made for a different trip.
A change to the day only (its length, the places, kosher, the starting point) re-plans the trails
that already passed agent 3, so agents 2-3 are skipped.
"""

from __future__ import annotations

from typing import Literal

from terramind.schemas import PreviousTurn, TripRequest

Change = Literal["new_trip", "edit", "different_trail"]
Scope = Literal["search", "replan"]

# Fields that change which trails are found or whether they are safe -> agents 2 and 3 run again.
SEARCH_FIELDS: dict[str, str] = {
    "region": "אזור",
    "trip_date": "תאריך",
    "start_time": "שעת תחילת ההליכה",
    "difficulty": "קושי",
    "trail_types": "סוג מסלול",
    "max_length_km": "אורך",
    "max_duration_hours": "משך הליכה",
    "max_drive_minutes": "זמן נסיעה",
    "group": "הרכב הקבוצה",
    "must_have": "דרישות",
    "avoid": "דברים להימנע מהם",
}
# Fields that only shape the day around an already-safe trail -> agent 4 re-plans.
DAY_FIELDS: dict[str, str] = {
    "day_hours": "אורך היום",
    "want_places": "מקומות",
    "kosher_only": "כשרות",
    "origin": "נקודת יציאה",
    "depart_time": "שעת יציאה מהבית",  # agent 4 re-checks the new start with the safety rules
}


def _changed(prev: TripRequest, new: TripRequest, fields: dict[str, str]) -> list[str]:
    return [label for name, label in fields.items() if getattr(prev, name) != getattr(new, name)]


def decide_scope(previous: PreviousTurn | None, new: TripRequest, change: Change) -> tuple[Scope, str]:
    """(scope, a short Hebrew reason shown in the UI)."""
    if previous is None or previous.plan is None:
        return "search", "בקשה חדשה"
    if change == "new_trip":
        return "search", "טיול חדש"
    if change == "different_trail":
        return "search", "ביקשת מסלול אחר"
    searched = _changed(previous.request, new, SEARCH_FIELDS)
    if searched:
        return "search", "השתנה: " + ", ".join(searched)
    day = _changed(previous.request, new, DAY_FIELDS)
    return "replan", ("שינוי ביום בלבד: " + ", ".join(day)) if day else "עדכון לתוכנית"
