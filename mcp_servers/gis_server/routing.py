"""Road routing (OSRM) and address lookup (Nominatim) behind the gis-server tools.

Both are external HTTP services: explicit timeouts, bounded retries (transport level), and a typed
fallback instead of an exception. OSRM down -> the straight-line estimate; Nominatim down -> an
`unavailable` result the UI can show.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Any

import httpx

from mcp_servers.common.http import build_client
from terramind.config import get_settings
from terramind.schemas import LegEstimate
from terramind.schemas.base import LatLon

log = logging.getLogger("terramind.mcp.gis.routing")

_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = build_client()
    return _client


def set_client(client: httpx.AsyncClient | None) -> None:
    """Tests inject a client (e.g. one routed by respx)."""
    global _client
    _client = client


async def osrm_drive(a: LatLon, b: LatLon) -> LegEstimate | None:
    """Fastest road route from a to b, or None when OSRM cannot answer (caller falls back)."""
    url = f"{get_settings().osrm_url}/route/v1/driving/{a[1]},{a[0]};{b[1]},{b[0]}"
    try:
        resp = await _http().get(url, params={"overview": "simplified", "geometries": "geojson"})
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        if data.get("code") != "Ok" or not data.get("routes"):
            log.info("OSRM found no route: %s", data.get("code"))
            return None
        route = data["routes"][0]
        coords = route["geometry"]["coordinates"]  # [lon, lat] pairs
        return LegEstimate(
            mode="drive",
            distance_km=round(route["distance"] / 1000, 1),
            minutes=max(1, round(route["duration"] / 60)),
            method="routing_engine",
            route=[(round(lat, 5), round(lon, 5)) for lon, lat in coords],
        )
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        log.warning("OSRM unavailable, using the estimate: %s", type(exc).__name__)
        return None


# --- geocoding -----------------------------------------------------------------------------------

_CACHE_MAX = 256
_geo_cache: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
_geo_lock = asyncio.Lock()
_last_call = 0.0
MIN_INTERVAL_S = 1.0  # Nominatim usage policy: at most one request per second


def _label(item: dict[str, Any]) -> str:
    """A short Hebrew-first label: the first parts of display_name ("רחוב הרצל 5, חיפה")."""
    parts = [p.strip() for p in str(item.get("display_name", "")).split(",") if p.strip()]
    return ", ".join(parts[:3]) or str(item.get("name", "?"))


async def geocode(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Places in Israel matching an address or place name. Raises httpx.HTTPError on failure."""
    key = " ".join(query.lower().split())
    if key in _geo_cache:
        _geo_cache.move_to_end(key)
        return _geo_cache[key][:limit]
    global _last_call
    async with _geo_lock:  # serializes requests, so the rate limit holds across concurrent users
        wait = MIN_INTERVAL_S - (time.monotonic() - _last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            resp = await _http().get(
                get_settings().nominatim_url,
                params={
                    "q": query,
                    "format": "jsonv2",
                    "countrycodes": "il",
                    "limit": 5,
                    "accept-language": "he",
                },
            )
        finally:
            _last_call = time.monotonic()
    resp.raise_for_status()
    places: list[dict[str, Any]] = []
    seen: set[tuple[str, float, float]] = set()
    for i in resp.json():
        if "lat" not in i or "lon" not in i:
            continue
        label, lat, lon = _label(i), round(float(i["lat"]), 5), round(float(i["lon"]), 5)
        if (label, lat, lon) not in seen:  # Nominatim can return the same place twice (node + relation)
            seen.add((label, lat, lon))
            places.append({"label": label, "lat": lat, "lon": lon})
    _geo_cache[key] = places
    if len(_geo_cache) > _CACHE_MAX:
        _geo_cache.popitem(last=False)
    return places[:limit]
