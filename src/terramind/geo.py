"""Distance and time helpers shared by the GIS server and the agents."""

from __future__ import annotations

import math
from datetime import time

EARTH_RADIUS_KM = 6371.0
WALK_DETOUR = 1.3  # trail distance vs straight line
DRIVE_DETOUR = 1.4  # road distance vs straight line (fallback when OSRM is unavailable)
DRIVE_KMH = 50.0  # average on rural northern roads (same fallback)
TERRAIN_FACTOR = 1.15  # real trails are slower than flat walking
DWELL_MINUTES = 15  # time spent at each stop
MAX_STOPS = 5
DEFAULT_START = time(8, 0)


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


def walk_minutes(km: float, pace_kmh: float) -> int:
    return math.ceil(km / pace_kmh * 60)


def hike_minutes(km: float, pace_kmh: float) -> int:
    """Walking time on the trail itself, without stops."""
    return round(walk_minutes(km, pace_kmh) * TERRAIN_FACTOR)


def max_outing_minutes(km: float, pace_kmh: float) -> int:
    """Upper bound of a whole outing (walking + the most stops agent 4 may add). Agent 3 checks
    daylight and heat against this, so whatever agent 4 plans afterwards is already covered."""
    return hike_minutes(km, pace_kmh) + MAX_STOPS * DWELL_MINUTES
