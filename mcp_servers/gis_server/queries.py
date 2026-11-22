"""PostGIS queries behind the gis-server tools. Pure data access, no MCP concerns."""

from __future__ import annotations

import json
import math
from typing import Any, Literal

import asyncpg

from terramind.geo import DRIVE_DETOUR, DRIVE_KMH, WALK_DETOUR, haversine_km
from terramind.regions import Region
from terramind.schemas import Difficulty, LegEstimate, PointOfInterest, TrailCandidate

# Requested difficulty -> acceptable trail difficulties (an "easy" request never gets "hard").
DIFFICULTY_ALLOWED: dict[Difficulty, list[str]] = {
    Difficulty.EASY: ["easy"],
    Difficulty.MODERATE: ["easy", "moderate"],
    Difficulty.HARD: ["easy", "moderate", "hard"],
}

SEARCH_SQL = """
SELECT t.trail_id, t.name, t.name_he, t.region, t.length_km, t.elevation_gain_m, t.difficulty,
       t.tags, t.is_loop, ST_Y(t.trailhead) AS lat, ST_X(t.trailhead) AS lon,
       CASE WHEN $9::float8 IS NULL THEN NULL
            ELSE ST_Distance(t.trailhead::geography,
                             ST_SetSRID(ST_MakePoint($10, $9), 4326)::geography) / 1000.0 END AS distance_km,
       cardinality(ARRAY(SELECT unnest(t.tags) INTERSECT SELECT unnest($5::text[]))) AS tag_hits
FROM trails t
WHERE ($1::float8 IS NULL OR ST_Intersects(t.geom, ST_MakeEnvelope($2, $1, $4, $3, 4326)))
  AND ($6::text[] IS NULL OR t.difficulty = ANY($6::text[]) OR t.difficulty IS NULL)
  AND ($7::float8 IS NULL OR t.length_km >= $7)
  AND ($8::float8 IS NULL OR t.length_km <= $8)
  AND ($9::float8 IS NULL OR $11::float8 IS NULL
       OR ST_DWithin(t.trailhead::geography, ST_SetSRID(ST_MakePoint($10, $9), 4326)::geography, $11 * 1000))
  AND (NOT $12::bool OR t.is_loop IS TRUE)
  AND NOT (t.trail_id = ANY($13::text[]))
ORDER BY tag_hits DESC, distance_km ASC NULLS LAST, abs(t.length_km - coalesce($14, t.length_km)) ASC
LIMIT $15
"""


async def search_trails(
    conn: asyncpg.Connection | asyncpg.Pool,
    *,
    region: Region | None,
    difficulty: Difficulty | None,
    min_km: float | None,
    max_km: float | None,
    tags: list[str],
    loop_only: bool,
    exclude_ids: list[str],
    near: tuple[float, float] | None,
    max_distance_km: float | None,
    target_km: float | None,
    limit: int,
) -> list[TrailCandidate]:
    s, w, n, e = region.bbox if region else (None, None, None, None)
    lat, lon = near if near else (None, None)
    rows = await conn.fetch(
        SEARCH_SQL,
        s,
        w,
        n,
        e,
        tags,
        DIFFICULTY_ALLOWED[difficulty] if difficulty else None,
        min_km,
        max_km,
        lat,
        lon,
        max_distance_km,
        loop_only,
        exclude_ids,
        target_km,
        limit,
    )
    return [
        TrailCandidate(
            trail_id=r["trail_id"],
            name=r["name"],
            name_he=r["name_he"],
            region=r["region"],
            length_km=round(r["length_km"], 2),
            elevation_gain_m=r["elevation_gain_m"],
            difficulty=r["difficulty"],
            tags=list(r["tags"]),
            is_loop=r["is_loop"],
            trailhead=(r["lat"], r["lon"]),
            distance_km=round(r["distance_km"], 1) if r["distance_km"] is not None else None,
            spatial_rank=i + 1,
        )
        for i, r in enumerate(rows)
    ]


DETAILS_SQL = """
SELECT t.trail_id, t.name, t.name_he, t.region, t.length_km, t.elevation_gain_m, t.difficulty, t.tags,
       t.is_loop, ST_Y(t.trailhead) AS lat, ST_X(t.trailhead) AS lon, t.osmc_symbol, t.colour,
       ST_Y(ST_EndPoint(ST_GeometryN(t.geom, ST_NumGeometries(t.geom)))) AS end_lat,
       ST_X(ST_EndPoint(ST_GeometryN(t.geom, ST_NumGeometries(t.geom)))) AS end_lon
FROM trails t WHERE t.trail_id = $1
"""


async def get_trail_details(conn: asyncpg.Connection | asyncpg.Pool, trail_id: str) -> dict[str, Any] | None:
    r = await conn.fetchrow(DETAILS_SQL, trail_id)
    if r is None:
        return None
    return {
        "trail": TrailCandidate(
            trail_id=r["trail_id"],
            name=r["name"],
            name_he=r["name_he"],
            region=r["region"],
            length_km=round(r["length_km"], 2),
            elevation_gain_m=r["elevation_gain_m"],
            difficulty=r["difficulty"],
            tags=list(r["tags"]),
            is_loop=r["is_loop"],
            trailhead=(r["lat"], r["lon"]),
        ),
        "osmc_symbol": r["osmc_symbol"],
        "colour": r["colour"],
        "trail_end": (r["end_lat"], r["end_lon"]),
    }


# Distance of each POI from the trailhead ALONG the trail line when the relation merges into a
# single line (measured from whichever end the trailhead is at), else straight-line distance.
POIS_SQL = """
WITH t AS (
    SELECT trail_id, geom, trailhead, ST_LineMerge(geom) AS line FROM trails WHERE trail_id = $1
), m AS (
    SELECT t.*, GeometryType(line) = 'LINESTRING' AS single,
           CASE WHEN GeometryType(line) = 'LINESTRING' THEN ST_Length(line::geography) END AS len_m,
           CASE WHEN GeometryType(line) = 'LINESTRING'
                THEN ST_Distance(trailhead, ST_EndPoint(line)) < ST_Distance(trailhead, ST_StartPoint(line))
           END AS reversed
    FROM t
)
SELECT p.poi_id, coalesce(p.name_he, p.name) AS name, p.category, ST_Y(p.geom) AS lat, ST_X(p.geom) AS lon,
       CASE WHEN m.single THEN
            m.len_m * CASE WHEN m.reversed THEN 1 - ST_LineLocatePoint(m.line, p.geom)
                           ELSE ST_LineLocatePoint(m.line, p.geom) END
       END AS along_m,
       ST_Distance(p.geom::geography, m.trailhead::geography) AS from_start_m
FROM m
JOIN pois p ON ST_DWithin(m.geom::geography, p.geom::geography, $2)
WHERE ($3::text[] IS NULL OR p.category = ANY($3::text[]))
ORDER BY coalesce(
    CASE WHEN m.single THEN m.len_m * CASE WHEN m.reversed THEN 1 - ST_LineLocatePoint(m.line, p.geom)
                                           ELSE ST_LineLocatePoint(m.line, p.geom) END END,
    ST_Distance(p.geom::geography, m.trailhead::geography))
LIMIT $4
"""


async def find_pois_near_trail(
    conn: asyncpg.Connection | asyncpg.Pool,
    trail_id: str,
    buffer_m: float,
    categories: list[str] | None,
    limit: int,
) -> list[PointOfInterest]:
    rows = await conn.fetch(POIS_SQL, trail_id, buffer_m, categories, limit)
    return [
        PointOfInterest(
            poi_id=r["poi_id"],
            name=r["name"],
            category=r["category"],
            location=(r["lat"], r["lon"]),
            distance_m=round(r["along_m"] if r["along_m"] is not None else r["from_start_m"]),
            along_trail=r["along_m"] is not None,
        )
        for r in rows
    ]


GEOJSON_SQL = """
SELECT ST_AsGeoJSON(geom, 6) AS geometry, trail_id, name, name_he, length_km, difficulty, tags
FROM trails WHERE trail_id = $1
"""


async def get_trail_geojson(conn: asyncpg.Connection | asyncpg.Pool, trail_id: str) -> dict[str, Any] | None:
    r = await conn.fetchrow(GEOJSON_SQL, trail_id)
    if r is None:
        return None
    return {
        "type": "Feature",
        "geometry": json.loads(r["geometry"]),
        "properties": {
            "trail_id": r["trail_id"],
            "name": r["name"],
            "name_he": r["name_he"],
            "length_km": round(r["length_km"], 2),
            "difficulty": r["difficulty"],
            "tags": list(r["tags"]),
        },
    }


def estimate_leg(
    a: tuple[float, float], b: tuple[float, float], mode: Literal["walk", "drive"], pace_kmh: float
) -> LegEstimate:
    straight = haversine_km(a, b)
    if mode == "walk":
        km = straight * WALK_DETOUR
        minutes = math.ceil(km / pace_kmh * 60)
    else:
        km = straight * DRIVE_DETOUR
        minutes = math.ceil(km / DRIVE_KMH * 60)
    return LegEstimate(mode=mode, distance_km=round(km, 2), minutes=minutes)
