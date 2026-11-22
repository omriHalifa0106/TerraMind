"""Named sub-regions (with Hebrew aliases) used to filter trails spatially by bounding box."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Region:
    key: str
    name_en: str
    name_he: str
    bbox: tuple[float, float, float, float]  # (south, west, north, east)
    aliases: tuple[str, ...] = ()


REGIONS: tuple[Region, ...] = (
    Region(
        "upper_galilee",
        "Upper Galilee",
        "גליל עליון",
        (32.90, 35.10, 33.33, 35.62),
        ("galilee", "upper galilee", "western galilee", "north", "גליל", "הגליל", "צפון", "גליל מערבי"),
    ),
    Region(
        "golan",
        "Golan Heights",
        "רמת הגולן",
        (32.70, 35.60, 33.33, 35.90),
        ("golan", "golan heights", "גולן", "הגולן"),
    ),
    Region(
        "meron",
        "Mount Meron area",
        "אזור מירון",
        (32.93, 35.33, 33.05, 35.50),
        ("meron", "מירון", "הר מירון"),
    ),
)


def resolve_region(text: str | None) -> Region | None:
    """Map free text ('the Golan', 'גליל עליון') to a known region; None if unknown."""
    if not text:
        return None
    needle = text.strip().lower().removeprefix("the ")
    for region in REGIONS:
        names = {region.key, region.name_en.lower(), region.name_he, *region.aliases}
        if needle in names:
            return region
    for region in REGIONS:  # looser: substring match
        if any(alias in needle for alias in (region.name_en.lower(), region.name_he, *region.aliases)):
            return region
    return None


def region_of(point: tuple[float, float]) -> Region | None:
    """First region whose bounding box contains (lat, lon); broad regions are listed first."""
    lat, lon = point
    for region in REGIONS:
        s, w, n, e = region.bbox
        if s <= lat <= n and w <= lon <= e:
            return region
    return None
