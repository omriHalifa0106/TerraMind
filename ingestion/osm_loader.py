"""Load hiking route relations and points of interest from OpenStreetMap into PostGIS.

Usage:
    uv run python -m ingestion.osm_loader                 # fetch from Overpass (cached)
    uv run python -m ingestion.osm_loader --refresh       # ignore cache
    uv run python -m ingestion.osm_loader --tiles 3       # 3x3 smaller queries (busy servers)
    uv run python -m ingestion.osm_loader --file x.json   # load a saved Overpass response

Israeli marked trails are mapped as `route=hiking` relations whose members are ways;
`out geom` returns each member way's coordinates inline, so no extra node lookup is needed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncpg
import httpx

from terramind.config import Settings, get_settings
from terramind.db import DatabaseSetupError, connect

log = logging.getLogger("terramind.ingestion.osm")
CACHE_DIR = Path(__file__).resolve().parent / "cache"

# OSM tag -> our POI category. The first matching rule wins.
MAX_GRID = 4  # up to 16 tiles
RETRY_STATUSES = {429, 502, 503, 504}  # rate limited / overloaded: try another mirror
OVERPASS_ROUNDS = 2

POI_RULES: list[tuple[str, str, str]] = [
    ("natural", "spring", "spring"),
    ("waterway", "waterfall", "waterfall"),
    ("tourism", "viewpoint", "viewpoint"),
    ("amenity", "parking", "parking"),
    ("tourism", "picnic_site", "picnic"),
    ("leisure", "picnic_table", "picnic"),
    ("historic", "ruins", "ruins"),
    ("historic", "archaeological_site", "ruins"),
    ("historic", "castle", "ruins"),
    ("amenity", "drinking_water", "drinking_water"),
]


def build_query(bbox: tuple[float, float, float, float], timeout_s: int = 180) -> str:
    s, w, n, e = bbox
    b = f"({s},{w},{n},{e})"
    poi_clauses = "\n".join(f'  node["{k}"="{v}"]{b};' for k, v, _ in POI_RULES)
    return (
        f"[out:json][timeout:{timeout_s}];\n"
        f'relation["route"="hiking"]{b};\n'
        "out geom;\n"  # body + member geometry ("out tags" would drop the members)
        f"(\n{poi_clauses}\n);\n"
        "out body;\n"
    )


@dataclass(frozen=True)
class TrailRow:
    trail_id: str
    osm_type: str
    osm_id: int
    name: str
    name_he: str | None
    name_en: str | None
    wkt: str
    trailhead_wkt: str
    osmc_symbol: str | None
    colour: str | None
    raw_tags: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PoiRow:
    poi_id: str
    osm_id: int
    category: str
    name: str | None
    name_he: str | None
    lon: float
    lat: float
    raw_tags: dict[str, str] = field(default_factory=dict)


def _line_wkt(points: list[dict[str, float]]) -> str:
    return "(" + ", ".join(f"{p['lon']} {p['lat']}" for p in points) + ")"


def parse_relation(el: dict[str, Any]) -> TrailRow | None:
    tags: dict[str, str] = el.get("tags", {})
    name = tags.get("name") or tags.get("name:he") or tags.get("name:en") or tags.get("ref")
    if not name:
        return None
    lines = [
        m["geometry"]
        for m in el.get("members", [])
        if m.get("type") == "way" and len(m.get("geometry") or []) >= 2
    ]
    if not lines:
        return None
    first = lines[0][0]
    return TrailRow(
        trail_id=f"osm-relation-{el['id']}",
        osm_type="relation",
        osm_id=int(el["id"]),
        name=name,
        name_he=tags.get("name:he") or (name if any("֐" <= c <= "׿" for c in name) else None),
        name_en=tags.get("name:en"),
        wkt="MULTILINESTRING(" + ", ".join(_line_wkt(line) for line in lines) + ")",
        trailhead_wkt=f"POINT({first['lon']} {first['lat']})",
        osmc_symbol=tags.get("osmc:symbol"),
        colour=tags.get("colour"),
        raw_tags=tags,
    )


def parse_node(el: dict[str, Any]) -> PoiRow | None:
    tags: dict[str, str] = el.get("tags", {})
    category = next((cat for k, v, cat in POI_RULES if tags.get(k) == v), None)
    if category is None or "lat" not in el:
        return None
    return PoiRow(
        poi_id=f"osm-node-{el['id']}",
        osm_id=int(el["id"]),
        category=category,
        name=tags.get("name"),
        name_he=tags.get("name:he"),
        lon=float(el["lon"]),
        lat=float(el["lat"]),
        raw_tags=tags,
    )


def parse_overpass(payload: dict[str, Any]) -> tuple[list[TrailRow], list[PoiRow]]:
    trails: list[TrailRow] = []
    pois: list[PoiRow] = []
    for el in payload.get("elements", []):
        if el.get("type") == "relation":
            if (t := parse_relation(el)) is not None:
                trails.append(t)
        elif el.get("type") == "node" and (p := parse_node(el)) is not None:
            pois.append(p)
    return trails, pois


async def fetch_overpass(
    settings: Settings, *, refresh: bool = False, bbox: tuple[float, float, float, float] | None = None
) -> dict[str, Any]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    bbox = bbox or settings.region_bbox
    s, w, n, e = (round(x, 4) for x in bbox)
    cache_file = CACHE_DIR / f"overpass_{s}_{w}_{n}_{e}.json"
    if cache_file.exists() and not refresh:
        log.info("using cached Overpass response %s", cache_file.name)
        data: dict[str, Any] = json.loads(cache_file.read_text(encoding="utf-8"))
        return data

    query = build_query(bbox)
    # Overpass can be slow for a region this size; read timeout is generous but bounded.
    timeout = httpx.Timeout(connect=10.0, read=240.0, write=10.0, pool=10.0)
    failures: list[str] = []
    async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "TerraMind/0.1 (education)"}) as c:
        for attempt in range(OVERPASS_ROUNDS):
            for url in settings.overpass_urls:  # rotate mirrors before waiting
                host = httpx.URL(url).host
                log.info("querying Overpass %s (a large region can take 1-4 minutes)...", host)
                try:
                    resp = await c.post(url, data={"data": query})
                except httpx.TransportError as exc:
                    failures.append(f"{host}: {type(exc).__name__}")
                    log.warning("Overpass %s unreachable (%s), trying next mirror", host, type(exc).__name__)
                    continue
                if resp.status_code in RETRY_STATUSES:
                    failures.append(f"{host}: HTTP {resp.status_code}")
                    log.warning("Overpass %s returned %s (busy), trying next mirror", host, resp.status_code)
                    continue
                resp.raise_for_status()
                data = resp.json()
                if data.get("remark", "").lower().startswith("runtime error"):
                    failures.append(f"{host}: {data['remark'][:80]}")  # server-side timeout / out of memory
                    continue
                cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                log.info("Overpass %s: %d elements", host, len(data.get("elements", [])))
                return data
            if attempt < OVERPASS_ROUNDS - 1:
                wait = 30 * (attempt + 1)
                log.warning(
                    "all Overpass mirrors busy, waiting %ss (round %d/%d)", wait, attempt + 1, OVERPASS_ROUNDS
                )
                await asyncio.sleep(wait)
    raise RuntimeError(
        "All Overpass mirrors refused the request (" + "; ".join(failures[-6:]) + "). "
        "Try again in a few minutes, or download the data yourself and load it with --file."
    )


def split_bbox(bbox: tuple[float, float, float, float], n: int) -> list[tuple[float, float, float, float]]:
    """n x n grid of sub-boxes (south, west, north, east) covering bbox."""
    s, w, north, e = bbox
    dlat, dlon = (north - s) / n, (e - w) / n
    return [
        (s + i * dlat, w + j * dlon, s + (i + 1) * dlat, w + (j + 1) * dlon)
        for i in range(n)
        for j in range(n)
    ]


def merge_payloads(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    """Union of elements; a relation crossing tiles appears in several of them, keep it once."""
    seen: dict[tuple[str, int], dict[str, Any]] = {}
    for p in payloads:
        for el in p.get("elements", []):
            seen.setdefault((el["type"], el["id"]), el)
    return {"elements": list(seen.values())}


async def fetch_region(settings: Settings, *, refresh: bool, tiles: int) -> dict[str, Any]:
    """Fetch the region in one query, or as a grid of smaller queries when servers time out.

    Each tile is cached on its own, so an interrupted run resumes where it stopped.
    """
    grid = tiles
    while True:
        boxes = split_bbox(settings.region_bbox, grid)
        if grid > 1:
            log.info("fetching the region as %d tiles (%dx%d)", len(boxes), grid, grid)
        try:
            payloads = []
            for i, box in enumerate(boxes, start=1):
                if grid > 1:
                    log.info("tile %d/%d", i, len(boxes))
                payloads.append(await fetch_overpass(settings, refresh=refresh, bbox=box))
            return merge_payloads(payloads)
        except RuntimeError:
            if grid >= MAX_GRID:
                raise
            grid += 1
            log.warning("query too heavy for the public servers; retrying with a %dx%d grid", grid, grid)


UPSERT_TRAIL = """
INSERT INTO trails (trail_id, osm_type, osm_id, name, name_he, name_en, region, geom, trailhead,
                    length_km, osmc_symbol, colour, raw_tags, updated_at)
VALUES ($1, $2, $3, $4, $5, $6, $7,
        ST_Multi(ST_GeomFromText($8, 4326)), ST_GeomFromText($9, 4326),
        ST_Length(ST_GeomFromText($8, 4326)::geography) / 1000.0,
        $10, $11, $12::jsonb, now())
ON CONFLICT (trail_id) DO UPDATE SET
    name = EXCLUDED.name, name_he = EXCLUDED.name_he, name_en = EXCLUDED.name_en,
    region = EXCLUDED.region, geom = EXCLUDED.geom, trailhead = EXCLUDED.trailhead,
    length_km = EXCLUDED.length_km, osmc_symbol = EXCLUDED.osmc_symbol,
    colour = EXCLUDED.colour, raw_tags = EXCLUDED.raw_tags, updated_at = now()
"""

UPSERT_POI = """
INSERT INTO pois (poi_id, osm_type, osm_id, category, name, name_he, geom, raw_tags)
VALUES ($1, 'node', $2, $3, $4, $5, ST_SetSRID(ST_MakePoint($6, $7), 4326), $8::jsonb)
ON CONFLICT (poi_id) DO UPDATE SET
    category = EXCLUDED.category, name = EXCLUDED.name, name_he = EXCLUDED.name_he,
    geom = EXCLUDED.geom, raw_tags = EXCLUDED.raw_tags
"""


async def load_into_db(
    conn: asyncpg.Connection, trails: list[TrailRow], pois: list[PoiRow], region: str
) -> tuple[int, int]:
    async with conn.transaction():
        await conn.executemany(
            UPSERT_TRAIL,
            [
                (
                    t.trail_id,
                    t.osm_type,
                    t.osm_id,
                    t.name,
                    t.name_he,
                    t.name_en,
                    region,
                    t.wkt,
                    t.trailhead_wkt,
                    t.osmc_symbol,
                    t.colour,
                    json.dumps(t.raw_tags, ensure_ascii=False),
                )
                for t in trails
            ],
        )
        await conn.executemany(
            UPSERT_POI,
            [
                (
                    p.poi_id,
                    p.osm_id,
                    p.category,
                    p.name,
                    p.name_he,
                    p.lon,
                    p.lat,
                    json.dumps(p.raw_tags, ensure_ascii=False),
                )
                for p in pois
            ],
        )
    return len(trails), len(pois)


async def run(file: Path | None, refresh: bool, dsn: str | None = None, tiles: int = 1) -> tuple[int, int]:
    settings = get_settings()
    if file is not None:
        payload: dict[str, Any] = json.loads(await asyncio.to_thread(file.read_text, encoding="utf-8"))
    else:
        payload = await fetch_region(settings, refresh=refresh, tiles=tiles)
    trails, pois = parse_overpass(payload)
    relations = sum(1 for el in payload.get("elements", []) if el.get("type") == "relation")
    if relations and not trails:
        raise RuntimeError(
            f"Overpass returned {relations} hiking relations but none had member geometry. "
            "The cached response is probably from an older query: re-run with --refresh."
        )
    conn = await connect(dsn or settings.database_url)
    try:
        return await load_into_db(conn, trails, pois, settings.region_name)
    finally:
        await conn.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", type=Path, help="Load a saved Overpass JSON instead of fetching")
    ap.add_argument("--refresh", action="store_true", help="Ignore the local cache")
    ap.add_argument("--tiles", type=int, default=1, help="Start with an NxN grid of smaller queries")
    args = ap.parse_args()
    n_trails, n_pois = asyncio.run(run(args.file, args.refresh, tiles=max(1, args.tiles)))
    print(f"loaded trails={n_trails} pois={n_pois}")


def cli() -> None:
    try:
        main()
    except (DatabaseSetupError, RuntimeError) as exc:
        raise SystemExit(f"ERROR: {exc}") from None


if __name__ == "__main__":
    cli()
