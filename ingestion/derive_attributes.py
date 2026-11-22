"""Derive trail attributes from geometry and nearby POIs (idempotent; safe to re-run).

- is_loop: start and end of the trail are within 250 m.
- tags: water / viewpoint / history from POIs within 200 m of the trail line, plus loop / family.
- difficulty: a length heuristic until elevation data (SRTM) is added.
- trailhead: for trails that merge into one line, the end closer to a parking lot (within 1.5 km),
  else the line's start. Stop times are measured along the line from this point.
"""

from __future__ import annotations

import asyncio

import asyncpg

from terramind.config import get_settings
from terramind.db import DatabaseSetupError, connect

DERIVE_SQL = """
WITH ends AS (
    SELECT trail_id,
           ST_StartPoint(ST_LineMerge(geom)) AS p0,
           ST_EndPoint(ST_LineMerge(geom))   AS p1
    FROM trails
),
near AS (
    SELECT t.trail_id,
           bool_or(p.category IN ('spring', 'waterfall'))          AS has_water,
           bool_or(p.category = 'viewpoint')                       AS has_view,
           bool_or(p.category = 'ruins')                           AS has_history
    FROM trails t
    JOIN pois p ON ST_DWithin(t.geom::geography, p.geom::geography, 200)
    GROUP BY t.trail_id
)
UPDATE trails t SET
    is_loop = CASE
        WHEN e.p0 IS NULL OR e.p1 IS NULL THEN NULL       -- disjoint multi-part relation
        ELSE ST_DWithin(e.p0::geography, e.p1::geography, 250)
    END,
    difficulty = CASE
        WHEN t.length_km < 6 THEN 'easy'
        WHEN t.length_km < 12 THEN 'moderate'
        ELSE 'hard'
    END,
    tags = ARRAY(
        SELECT DISTINCT x FROM unnest(ARRAY[
            CASE WHEN n.has_water   THEN 'water' END,
            CASE WHEN n.has_view    THEN 'viewpoint' END,
            CASE WHEN n.has_history THEN 'history' END,
            CASE WHEN e.p0 IS NOT NULL AND e.p1 IS NOT NULL
                  AND ST_DWithin(e.p0::geography, e.p1::geography, 250) THEN 'loop' END,
            CASE WHEN t.length_km < 5 THEN 'family' END
        ]) AS x WHERE x IS NOT NULL ORDER BY x
    ),
    updated_at = now()
FROM ends e
LEFT JOIN near n ON n.trail_id = e.trail_id
WHERE t.trail_id = e.trail_id
"""


TRAILHEAD_SQL = """
WITH m AS (
    SELECT trail_id, ST_LineMerge(geom) AS line FROM trails
), e AS (
    SELECT trail_id, ST_StartPoint(line) AS p0, ST_EndPoint(line) AS p1
    FROM m WHERE GeometryType(line) = 'LINESTRING'
), k AS (
    SELECT e.*,
           (SELECT min(ST_Distance(e.p0::geography, p.geom::geography)) FROM pois p
             WHERE p.category = 'parking' AND ST_DWithin(e.p0::geography, p.geom::geography, 1500)) AS d0,
           (SELECT min(ST_Distance(e.p1::geography, p.geom::geography)) FROM pois p
             WHERE p.category = 'parking' AND ST_DWithin(e.p1::geography, p.geom::geography, 1500)) AS d1
    FROM e
)
UPDATE trails t
SET trailhead = CASE WHEN k.d1 IS NOT NULL AND (k.d0 IS NULL OR k.d1 < k.d0) THEN k.p1 ELSE k.p0 END
FROM k WHERE t.trail_id = k.trail_id
"""


async def derive(conn: asyncpg.Connection) -> int:
    await conn.execute(TRAILHEAD_SQL)
    result: str = await conn.execute(DERIVE_SQL)
    return int(result.split()[-1])  # "UPDATE <n>"


async def run(dsn: str | None = None) -> int:
    conn = await connect(dsn or get_settings().database_url)
    try:
        return await derive(conn)
    finally:
        await conn.close()


def main() -> None:
    print("trails updated:", asyncio.run(run()))


def cli() -> None:
    try:
        main()
    except (DatabaseSetupError, RuntimeError) as exc:
        raise SystemExit(f"ERROR: {exc}") from None


if __name__ == "__main__":
    cli()
