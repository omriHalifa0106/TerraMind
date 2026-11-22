"""Open-Meteo requests and response parsing into WeatherSnapshot.

Two shapes are used: one point on one date (live fallback in the weather server), and many points
over the whole forecast horizon in one request (the pipeline: Open-Meteo accepts coordinate lists
and then answers with a JSON list, one entry per point).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from terramind.schemas import WeatherSnapshot

DAILY_VARS = (
    "temperature_2m_max,apparent_temperature_max,precipitation_sum,"
    "precipitation_probability_max,wind_gusts_10m_max,uv_index_max,sunrise,sunset"
)
HOURLY_VARS = "precipitation_probability"
TIMEZONE = "Asia/Jerusalem"


def grid_key(lat: float, lon: float) -> tuple[Decimal, Decimal]:
    """~1 km grid cell of a point: nearby trailheads share one forecast row."""
    return Decimal(f"{lat:.2f}"), Decimal(f"{lon:.2f}")


def build_params(lat: float, lon: float, for_date: date) -> dict[str, str | float]:
    return {
        "latitude": lat,
        "longitude": lon,
        "timezone": TIMEZONE,
        "start_date": for_date.isoformat(),
        "end_date": for_date.isoformat(),
        "daily": DAILY_VARS,
        "hourly": HOURLY_VARS,
    }


def build_batch_params(points: Sequence[tuple[Decimal, Decimal]], days: int) -> dict[str, str | int]:
    return {
        "latitude": ",".join(str(lat) for lat, _ in points),
        "longitude": ",".join(str(lon) for _, lon in points),
        "timezone": TIMEZONE,
        "forecast_days": days,
        "daily": DAILY_VARS,
        "hourly": HOURLY_VARS,
    }


def _at(daily: dict[str, list[Any]], key: str, i: int) -> Any:
    values = daily.get(key) or []
    return values[i] if i < len(values) else None


def _dt(value: Any) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) else None


def _snapshot(
    trail_id: str, for_date: date, daily: dict[str, list[Any]], i: int, hours: dict[int, int]
) -> WeatherSnapshot:
    prob_max = _at(daily, "precipitation_probability_max", i)
    return WeatherSnapshot(
        trail_id=trail_id,
        for_date=for_date,
        temp_max_c=_at(daily, "temperature_2m_max", i),
        apparent_temp_max_c=_at(daily, "apparent_temperature_max", i),
        precip_prob_max_pct=int(prob_max) if prob_max is not None else None,
        precip_sum_mm=_at(daily, "precipitation_sum", i),
        wind_gust_max_kmh=_at(daily, "wind_gusts_10m_max", i),
        uv_index_max=_at(daily, "uv_index_max", i),
        sunrise=_dt(_at(daily, "sunrise", i)),
        sunset=_dt(_at(daily, "sunset", i)),
        hourly_precip_prob=hours,
        status="ok",
    )


def _hours_by_date(hourly: dict[str, list[Any]]) -> dict[date, dict[int, int]]:
    out: dict[date, dict[int, int]] = {}
    for ts, prob in zip(hourly.get("time", []), hourly.get("precipitation_probability", []), strict=False):
        if prob is not None:
            t = datetime.fromisoformat(ts)
            out.setdefault(t.date(), {})[t.hour] = int(prob)
    return out


def parse_snapshot(trail_id: str, for_date: date, payload: dict[str, Any]) -> WeatherSnapshot:
    """A single-date response (build_params)."""
    daily: dict[str, list[Any]] = payload.get("daily") or {}
    hours = _hours_by_date(payload.get("hourly") or {})
    return _snapshot(trail_id, for_date, daily, 0, hours.get(for_date, {}))


def parse_days(payload: dict[str, Any]) -> list[WeatherSnapshot]:
    """All days of one point's response (one entry of a build_batch_params answer)."""
    daily: dict[str, list[Any]] = payload.get("daily") or {}
    hours = _hours_by_date(payload.get("hourly") or {})
    days = [date.fromisoformat(d) for d in daily.get("time", [])]
    return [_snapshot("grid", d, daily, i, hours.get(d, {})) for i, d in enumerate(days)]


def split_batch(payload: Any) -> list[dict[str, Any]]:
    """Open-Meteo answers a one-point request with an object and a multi-point one with a list."""
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    return [payload] if isinstance(payload, dict) else []
