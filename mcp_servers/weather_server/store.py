"""weather_forecasts table: written by the pipeline (and by live fallbacks), read by the weather server."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any

import asyncpg

from terramind.schemas import WeatherSnapshot

UPSERT = """
INSERT INTO weather_forecasts (
    lat_key, lon_key, for_date, temp_max_c, apparent_temp_max_c, precip_prob_max_pct, precip_sum_mm,
    wind_gust_max_kmh, uv_index_max, sunrise, sunset, hourly_precip_prob, fetched_at, run_id
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb, now(), $13)
ON CONFLICT (lat_key, lon_key, for_date) DO UPDATE SET
    temp_max_c = EXCLUDED.temp_max_c,
    apparent_temp_max_c = EXCLUDED.apparent_temp_max_c,
    precip_prob_max_pct = EXCLUDED.precip_prob_max_pct,
    precip_sum_mm = EXCLUDED.precip_sum_mm,
    wind_gust_max_kmh = EXCLUDED.wind_gust_max_kmh,
    uv_index_max = EXCLUDED.uv_index_max,
    sunrise = EXCLUDED.sunrise,
    sunset = EXCLUDED.sunset,
    hourly_precip_prob = EXCLUDED.hourly_precip_prob,
    fetched_at = now(),
    run_id = EXCLUDED.run_id
"""

SELECT_FRESH = """
SELECT * FROM weather_forecasts
WHERE lat_key = $1 AND lon_key = $2 AND for_date = $3
  AND fetched_at > now() - make_interval(secs => $4)
"""


def row_values(key: tuple[Decimal, Decimal], s: WeatherSnapshot, run_id: int | None) -> tuple[Any, ...]:
    return (
        key[0],
        key[1],
        s.for_date,
        s.temp_max_c,
        s.apparent_temp_max_c,
        s.precip_prob_max_pct,
        s.precip_sum_mm,
        s.wind_gust_max_kmh,
        s.uv_index_max,
        s.sunrise,
        s.sunset,
        json.dumps({str(h): p for h, p in s.hourly_precip_prob.items()}),
        run_id,
    )


def from_row(trail_id: str, row: asyncpg.Record) -> WeatherSnapshot:
    hourly = row["hourly_precip_prob"]
    hours = json.loads(hourly) if isinstance(hourly, str) else dict(hourly)
    return WeatherSnapshot(
        trail_id=trail_id,
        for_date=row["for_date"],
        temp_max_c=row["temp_max_c"],
        apparent_temp_max_c=row["apparent_temp_max_c"],
        precip_prob_max_pct=row["precip_prob_max_pct"],
        precip_sum_mm=row["precip_sum_mm"],
        wind_gust_max_kmh=row["wind_gust_max_kmh"],
        uv_index_max=row["uv_index_max"],
        sunrise=row["sunrise"],
        sunset=row["sunset"],
        hourly_precip_prob={int(h): int(p) for h, p in hours.items()},
        status="cached",  # served from the pipeline's stored data, not fetched for this request
    )


async def fetch_fresh(
    conn: asyncpg.Pool | asyncpg.Connection, key: tuple[Decimal, Decimal], for_date: date, max_age_s: float
) -> asyncpg.Record | None:
    row: asyncpg.Record | None = await conn.fetchrow(SELECT_FRESH, key[0], key[1], for_date, max_age_s)
    return row
