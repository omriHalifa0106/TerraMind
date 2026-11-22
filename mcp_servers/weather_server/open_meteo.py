"""Parse Open-Meteo forecast responses into WeatherSnapshot."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from terramind.schemas import WeatherSnapshot

DAILY_VARS = (
    "temperature_2m_max,apparent_temperature_max,precipitation_sum,"
    "precipitation_probability_max,wind_gusts_10m_max,uv_index_max,sunrise,sunset"
)
HOURLY_VARS = "precipitation_probability"


def build_params(lat: float, lon: float, for_date: date) -> dict[str, str | float]:
    return {
        "latitude": lat,
        "longitude": lon,
        "timezone": "Asia/Jerusalem",
        "start_date": for_date.isoformat(),
        "end_date": for_date.isoformat(),
        "daily": DAILY_VARS,
        "hourly": HOURLY_VARS,
    }


def _first(daily: dict[str, list[Any]], key: str) -> Any:
    values = daily.get(key) or [None]
    return values[0]


def _dt(value: Any) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) else None


def parse_snapshot(trail_id: str, for_date: date, payload: dict[str, Any]) -> WeatherSnapshot:
    daily: dict[str, list[Any]] = payload.get("daily") or {}
    hourly: dict[str, list[Any]] = payload.get("hourly") or {}
    hours: dict[int, int] = {}
    for ts, prob in zip(hourly.get("time", []), hourly.get("precipitation_probability", []), strict=False):
        if prob is not None:
            hours[datetime.fromisoformat(ts).hour] = int(prob)
    prob_max = _first(daily, "precipitation_probability_max")
    return WeatherSnapshot(
        trail_id=trail_id,
        for_date=for_date,
        temp_max_c=_first(daily, "temperature_2m_max"),
        apparent_temp_max_c=_first(daily, "apparent_temperature_max"),
        precip_prob_max_pct=int(prob_max) if prob_max is not None else None,
        precip_sum_mm=_first(daily, "precipitation_sum"),
        wind_gust_max_kmh=_first(daily, "wind_gusts_10m_max"),
        uv_index_max=_first(daily, "uv_index_max"),
        sunrise=_dt(_first(daily, "sunrise")),
        sunset=_dt(_first(daily, "sunset")),
        hourly_precip_prob=hours,
        status="ok",
    )
