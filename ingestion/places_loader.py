"""Load places for a whole day out (food, coffee carts, attractions, wineries, farms) from OpenStreetMap.

Trails and their own points of interest come from osm_loader; this adds the places a day plan can
stop at before or after the hike. They go into the same `pois` table with their own categories, and
their OSM tags (opening_hours, diet:kosher, cuisine, website) are kept in raw_tags.

    uv run python -m ingestion.places_loader            # fetch from Overpass (cached)
    uv run python -m ingestion.places_loader --refresh  # ignore the cache
    uv run python -m ingestion.places_loader --file x.json

Coffee carts: there is no public coffee-cart API in Israel, and OSM maps very few of them. A cart is a
cafe tagged street_vendor/mobile, or a cafe or kiosk whose name says it is a cart ("עגלת קפה").
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncpg
import httpx

from ingestion.osm_loader import CACHE_DIR, OVERPASS_ROUNDS, RETRY_STATUSES, merge_payloads, split_bbox
from terramind.config import Settings, get_settings
from terramind.db import DatabaseSetupError, connect

log = logging.getLogger("terramind.ingestion.places")

# Day-plan categories (the trail POI categories live in osm_loader.POI_RULES).
PLACE_CATEGORIES = ("restaurant", "cafe", "coffee_cart", "attraction", "museum", "winery", "brewery", "farm")

# OSM tag -> category, first match wins. Coffee carts are checked in code before plain cafes.
PLACE_RULES: list[tuple[str, str, str]] = [
    ("amenity", "restaurant", "restaurant"),
    ("amenity", "cafe", "cafe"),
    ("tourism", "museum", "museum"),
    ("tourism", "attraction", "attraction"),
    ("craft", "winery", "winery"),
    ("craft", "brewery", "brewery"),
    ("shop", "farm", "farm"),
    ("shop", "cheese", "farm"),  # Galilee dairies selling their own cheese
    ("shop", "kiosk", "kiosk"),  # kept only when it is a coffee cart (see _category)
]
CART_NAME = re.compile(r"עגל|coffee\s*cart|\bcart\b", re.IGNORECASE)


def build_query(bbox: tuple[float, float, float, float], timeout_s: int = 180) -> str:
    s, w, n, e = bbox
    b = f"({s},{w},{n},{e})"
    clauses = "\n".join(f'  nwr["{k}"="{v}"]["name"]{b};' for k, v, _ in PLACE_RULES)
    return f"[out:json][timeout:{timeout_s}];\n(\n{clauses}\n);\nout tags center;\n"


@dataclass(frozen=True)
class PlaceRow:
    poi_id: str
    osm_type: str
    osm_id: int
    category: str
    name: str
    name_he: str | None
    lon: float
    lat: float
    raw_tags: dict[str, str] = field(default_factory=dict)


def _category(tags: dict[str, str]) -> str | None:
    base = next((cat for k, v, cat in PLACE_RULES if tags.get(k) == v), None)
    name = " ".join(tags.get(k, "") for k in ("name", "name:he", "name:en"))
    is_cart = (
        tags.get("street_vendor") == "yes" or tags.get("mobile") == "yes" or bool(CART_NAME.search(name))
    )
    if base in ("cafe", "kiosk") and is_cart:
        return "coffee_cart"
    return None if base == "kiosk" else base  # a kiosk that is not a coffee cart is not a day-plan stop


def parse_element(el: dict[str, Any]) -> PlaceRow | None:
    tags: dict[str, str] = el.get("tags", {})
    category = _category(tags)
    name = tags.get("name:he") or tags.get("name") or tags.get("name:en")
    # Nodes carry lat/lon; ways and relations carry a computed "center" (out center).
    point = el if "lat" in el else el.get("center")
    if category is None or not name or not point:
        return None
    return PlaceRow(
        poi_id=f"osm-{el['type']}-{el['id']}",
        osm_type=el["type"],
        osm_id=int(el["id"]),
        category=category,
        name=tags.get("name") or name,
        name_he=tags.get("name:he"),
        lon=float(point["lon"]),
        lat=float(point["lat"]),
        raw_tags=tags,
    )


def parse_overpass(payload: dict[str, Any]) -> list[PlaceRow]:
    return [p for el in payload.get("elements", []) if (p := parse_element(el)) is not None]


async def fetch_tile(
    settings: Settings, bbox: tuple[float, float, float, float], refresh: bool
) -> dict[str, Any]:
    """One Overpass query for one tile, cached on its own (an interrupted run resumes)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    s, w, n, e = (round(x, 4) for x in bbox)
    cache_file = CACHE_DIR / f"places_{s}_{w}_{n}_{e}.json"
    if cache_file.exists() and not refresh:
        cached: dict[str, Any] = json.loads(cache_file.read_text(encoding="utf-8"))
        return cached
    query = build_query(bbox)
    timeout = httpx.Timeout(connect=10.0, read=200.0, write=10.0, pool=10.0)
    failures: list[str] = []
    async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "TerraMind/0.1 (education)"}) as c:
        for attempt in range(OVERPASS_ROUNDS):
            for url in settings.overpass_urls:
                host = httpx.URL(url).host
                try:
                    resp = await c.post(url, data={"data": query})
                except httpx.TransportError as exc:
                    failures.append(f"{host}: {type(exc).__name__}")
                    continue
                if resp.status_code in RETRY_STATUSES:
                    failures.append(f"{host}: HTTP {resp.status_code}")
                    continue
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()
                cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                log.info("Overpass %s: %d elements", host, len(data.get("elements", [])))
                return data
            if attempt < OVERPASS_ROUNDS - 1:
                await asyncio.sleep(30 * (attempt + 1))
    raise RuntimeError("All Overpass mirrors refused the request (" + "; ".join(failures[-6:]) + ")")


async def fetch(settings: Settings, refresh: bool, tiles: int) -> dict[str, Any]:
    """The region as a tiles x tiles grid of smaller queries (the public servers reject big ones)."""
    boxes = split_bbox(settings.region_bbox, tiles)
    payloads = []
    for i, box in enumerate(boxes, start=1):
        log.info("tile %d/%d", i, len(boxes))
        payloads.append(await fetch_tile(settings, box, refresh))
    return merge_payloads(payloads)


UPSERT = """
INSERT INTO pois (poi_id, osm_type, osm_id, category, name, name_he, geom, raw_tags)
VALUES ($1, $2, $3, $4, $5, $6, ST_SetSRID(ST_MakePoint($7, $8), 4326), $9::jsonb)
ON CONFLICT (poi_id) DO UPDATE SET
    category = EXCLUDED.category, name = EXCLUDED.name, name_he = EXCLUDED.name_he,
    geom = EXCLUDED.geom, raw_tags = EXCLUDED.raw_tags
"""


async def load_into_db(conn: asyncpg.Connection, places: list[PlaceRow]) -> int:
    async with conn.transaction():
        await conn.executemany(
            UPSERT,
            [
                (
                    p.poi_id,
                    p.osm_type,
                    p.osm_id,
                    p.category,
                    p.name,
                    p.name_he,
                    p.lon,
                    p.lat,
                    json.dumps(p.raw_tags, ensure_ascii=False),
                )
                for p in places
            ],
        )
    return len(places)


async def run(
    file: Path | None = None, refresh: bool = False, dsn: str | None = None, tiles: int = 3
) -> dict[str, int]:
    """Fetch (or read) and load. Returns the number of places per category."""
    settings = get_settings()
    if file is not None:
        payload: dict[str, Any] = json.loads(await asyncio.to_thread(file.read_text, encoding="utf-8"))
    else:
        payload = await fetch(settings, refresh, tiles)
    places = parse_overpass(payload)
    conn = await connect(dsn or settings.database_url)
    try:
        await load_into_db(conn, places)
    finally:
        await conn.close()
    counts: dict[str, int] = {}
    for p in places:
        counts[p.category] = counts.get(p.category, 0) + 1
    return counts


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", type=Path, help="Load a saved Overpass JSON instead of fetching")
    ap.add_argument("--refresh", action="store_true", help="Ignore the local cache")
    ap.add_argument("--tiles", type=int, default=3, help="Split the region into an NxN grid of queries")
    args = ap.parse_args()
    try:
        counts = asyncio.run(run(args.file, args.refresh, tiles=max(1, args.tiles)))
    except (DatabaseSetupError, RuntimeError) as exc:
        raise SystemExit(f"ERROR: {exc}") from None
    print("loaded places:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none")


if __name__ == "__main__":
    main()
