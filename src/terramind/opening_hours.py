"""Is a place open at a given moment? A small reader for the common OSM `opening_hours` formats.

Handles what most places in the region use: "24/7", "Su-Th 09:00-17:00; Fr 09:00-14:00; Sa off",
day lists ("Mo,We"), split hours ("12:00-15:00,18:00-23:00"), and hours past midnight ("18:00-02:00").
Anything else (holidays, months, sunrise, comments) is reported as unknown rather than guessed:
the planner shows "opening hours unknown" instead of sending a family to a closed winery.
"""

from __future__ import annotations

import re
from datetime import date, time
from typing import Literal

OpenState = Literal["open", "closed", "unknown"]

DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]  # date.weekday() order
_DAY_TOKEN = re.compile(r"^(Mo|Tu|We|Th|Fr|Sa|Su)(?:-(Mo|Tu|We|Th|Fr|Sa|Su))?$")
_SPAN = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$")


class _Unknown(ValueError):
    """A format this reader does not handle."""


def _days(selector: str) -> set[int]:
    out: set[int] = set()
    for part in selector.split(","):
        m = _DAY_TOKEN.match(part.strip())
        if not m:
            raise _Unknown(selector)
        a = DAYS.index(m.group(1))
        b = DAYS.index(m.group(2)) if m.group(2) else a
        i = a
        while True:  # ranges may wrap around the week: "Su-Th"
            out.add(i)
            if i == b:
                break
            i = (i + 1) % 7
    return out


def _minutes(h: str, m: str) -> int:
    return int(h) * 60 + int(m)


def _spans(text: str) -> list[tuple[int, int]]:
    spans = []
    for part in text.split(","):
        m = _SPAN.match(part.strip())
        if not m:
            raise _Unknown(text)
        start, end = _minutes(m.group(1), m.group(2)), _minutes(m.group(3), m.group(4))
        spans.append((start, end if end > start else end + 24 * 60))  # past midnight
    return spans


def _rules(spec: str) -> dict[int, list[tuple[int, int]]]:
    """weekday -> open spans in minutes. A later rule for the same day replaces an earlier one (OSM)."""
    week: dict[int, list[tuple[int, int]]] = {}
    for rule in (r.strip() for r in spec.split(";")):
        if not rule:
            continue
        if rule == "24/7":
            for d in range(7):
                week[d] = [(0, 24 * 60)]
            continue
        head, _, rest = rule.partition(" ")
        if _SPAN.match(head.split(",")[0]):  # times without days: every day
            days, rest = set(range(7)), rule
        else:
            days = _days(head)
        rest = rest.strip()
        spans = [] if rest in ("off", "closed") else _spans(rest)
        for d in days:
            week[d] = spans
    return week


def is_open(spec: str | None, on: date, at: time, minutes: int = 0) -> OpenState:
    """Whether the place is open from `at` for `minutes` on `on` (unknown when not mapped/readable)."""
    if not spec:
        return "unknown"
    try:
        week = _rules(spec)
    except _Unknown:
        return "unknown"
    if not week:
        return "unknown"
    start = at.hour * 60 + at.minute
    end = start + minutes
    # A weekday the spec does not mention is closed (OSM semantics: "Mo-Fr ..." means closed weekends).
    today = week.get(on.weekday(), [])
    yesterday = week.get((on.weekday() - 1) % 7, [])
    spans = today + [(s - 24 * 60, e - 24 * 60) for s, e in yesterday if e > 24 * 60]
    return "open" if any(s <= start and end <= e for s, e in spans) else "closed"


def opens_within(
    spec: str | None, on: date, at: time, minutes: int, max_wait: int, step: int = 1
) -> time | None:
    """The earliest time within max_wait minutes of `at` from which the place is open for `minutes`
    (None if it does not open in time, or its hours are unknown)."""
    start = at.hour * 60 + at.minute
    for wait in range(0, max_wait + 1, step):
        t = start + wait
        if t >= 24 * 60:
            return None
        if is_open(spec, on, time(t // 60, t % 60), minutes) == "open":
            return time(t // 60, t % 60)
    return None
