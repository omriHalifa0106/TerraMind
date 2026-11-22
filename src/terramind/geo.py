"""Small geodesic helpers shared by the GIS server and the planner."""

from __future__ import annotations

import math

EARTH_RADIUS_KM = 6371.0
WALK_DETOUR = 1.3  # trail distance vs straight line
DRIVE_DETOUR = 1.4  # road distance vs straight line
DRIVE_KMH = 50.0  # average on rural northern roads


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


def walk_minutes(km: float, pace_kmh: float) -> int:
    return math.ceil(km / pace_kmh * 60)


def drive_minutes(a: tuple[float, float], b: tuple[float, float]) -> int:
    return math.ceil(haversine_km(a, b) * DRIVE_DETOUR / DRIVE_KMH * 60)
