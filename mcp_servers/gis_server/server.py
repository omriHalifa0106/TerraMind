"""gis-server: spatial access to trails and points of interest (PostGIS) over MCP."""

from __future__ import annotations

import logging
from typing import Literal

import asyncpg
from fastmcp import FastMCP

from mcp_servers.common.db import get_pool
from mcp_servers.gis_server import queries
from terramind.regions import REGIONS, resolve_region
from terramind.schemas import Difficulty, LegEstimate
from terramind.schemas.tool_results import (
    GeoJsonResult,
    PoiResult,
    RegionInfo,
    RegionList,
    TrailDetails,
    TrailSearchResult,
)
from terramind.text import name_match_score

log = logging.getLogger("terramind.mcp.gis")
mcp = FastMCP("terramind-gis", instructions="Spatial search over hiking trails and POIs in northern Israel.")

DB_ERRORS = (asyncpg.PostgresError, OSError, TimeoutError)


@mcp.tool
async def list_regions() -> RegionList:
    """List the named regions that trails can be filtered by (English and Hebrew names)."""
    return RegionList(
        regions=[RegionInfo(key=r.key, name_en=r.name_en, name_he=r.name_he, bbox=r.bbox) for r in REGIONS]
    )


@mcp.tool
async def search_trails(
    region: str | None = None,
    difficulty: Difficulty | None = None,
    min_km: float | None = None,
    max_km: float | None = None,
    tags: list[str] | None = None,
    loop_only: bool = False,
    exclude_ids: list[str] | None = None,
    near_lat: float | None = None,
    near_lon: float | None = None,
    max_distance_km: float | None = None,
    target_km: float | None = None,
    limit: int = 10,
) -> TrailSearchResult:
    """Find hiking trails by region, difficulty, length, tags (water, viewpoint, history, loop, family)
    and distance from a point. Results are ranked by tag matches, then distance, then closeness to
    target_km. Trails in exclude_ids (e.g. vetoed by the safety critic) are never returned."""
    resolved = resolve_region(region)
    if region and resolved is None:
        return TrailSearchResult(status="empty", detail=f"Unknown region {region!r}; call list_regions")
    near = (near_lat, near_lon) if near_lat is not None and near_lon is not None else None
    try:
        pool = await get_pool()
        trails = await queries.search_trails(
            pool,
            region=resolved,
            difficulty=difficulty,
            min_km=min_km,
            max_km=max_km,
            tags=tags or [],
            loop_only=loop_only,
            exclude_ids=exclude_ids or [],
            near=near,
            max_distance_km=max_distance_km,
            target_km=target_km,
            limit=max(1, min(limit, 50)),
        )
    except DB_ERRORS as exc:
        log.exception("search_trails failed")
        return TrailSearchResult(status="error", detail=f"database error: {type(exc).__name__}")
    return TrailSearchResult(status="ok" if trails else "empty", trails=trails)


@mcp.tool
async def find_trails_by_name(text: str, limit: int = 5) -> TrailSearchResult:
    """Trails whose name is mentioned in free text (Hebrew or English), e.g. "hike Nahal Kziv tomorrow"
    or "לטייל בנחל כזיב". Generic words like Nahal/נחל are ignored, Hebrew prefixes are handled.
    fused_score holds the name-match score: 1.0 means every distinctive word of the name was mentioned."""
    try:
        pool = await get_pool()
        rows = await pool.fetch("SELECT trail_id, name, name_he, name_en FROM trails")
        scored = []
        for r in rows:
            score = max(
                (name_match_score(n, text) for n in (r["name"], r["name_he"], r["name_en"]) if n), default=0.0
            )
            if score > 0:
                scored.append((score, r["trail_id"]))
        scored.sort(reverse=True)
        trails = [
            d["trail"].model_copy(update={"fused_score": round(score, 3)})
            for score, tid in scored[: max(1, min(limit, 20))]
            if (d := await queries.get_trail_details(pool, tid)) is not None
        ]
    except DB_ERRORS as exc:
        return TrailSearchResult(status="error", detail=f"database error: {type(exc).__name__}")
    return TrailSearchResult(status="ok" if trails else "empty", trails=trails)


@mcp.tool
async def get_trail_details(trail_id: str) -> TrailDetails:
    """Full metadata for one trail, including its end point and OSM trail-marking symbol."""
    try:
        data = await queries.get_trail_details(await get_pool(), trail_id)
    except DB_ERRORS as exc:
        return TrailDetails(status="error", detail=f"database error: {type(exc).__name__}")
    if data is None:
        return TrailDetails(status="empty", detail=f"No trail {trail_id!r}")
    return TrailDetails(status="ok", **data)


@mcp.tool
async def find_pois_near_trail(
    trail_id: str, buffer_m: float = 300, categories: list[str] | None = None, limit: int = 20
) -> PoiResult:
    """Points of interest (spring, waterfall, viewpoint, parking, picnic, ruins, drinking_water)
    within buffer_m of the trail line, ordered by distance from the trailhead."""
    try:
        pois = await queries.find_pois_near_trail(
            await get_pool(), trail_id, min(buffer_m, 2000), categories, max(1, min(limit, 50))
        )
    except DB_ERRORS as exc:
        return PoiResult(status="error", detail=f"database error: {type(exc).__name__}")
    return PoiResult(status="ok" if pois else "empty", pois=pois)


@mcp.tool
async def estimate_leg(
    from_lat: float,
    from_lon: float,
    to_lat: float,
    to_lon: float,
    mode: Literal["walk", "drive"] = "walk",
    pace_kmh: float = 3.5,
) -> LegEstimate:
    """Estimated distance and minutes between two points (straight line x detour factor in the MVP)."""
    return queries.estimate_leg((from_lat, from_lon), (to_lat, to_lon), mode, pace_kmh)


@mcp.tool
async def get_trail_geojson(trail_id: str) -> GeoJsonResult:
    """GeoJSON Feature of the trail geometry, for drawing on a map."""
    try:
        feature = await queries.get_trail_geojson(await get_pool(), trail_id)
    except DB_ERRORS as exc:
        return GeoJsonResult(status="error", detail=f"database error: {type(exc).__name__}")
    if feature is None:
        return GeoJsonResult(status="empty", detail=f"No trail {trail_id!r}")
    return GeoJsonResult(status="ok", feature=feature)


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8101)
