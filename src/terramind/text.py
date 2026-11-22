"""Name matching for trails: core tokens without generic words, with Hebrew prefix stripping."""

from __future__ import annotations

import re

# Words that say what KIND of place or trail it is, not WHICH one. Two names that share only
# these ("Nahal Iyun" / "Nahal Amud", "Red trail to X" / "Red trail to Y") are not a match.
GENERIC_WORDS = frozenset(
    {
        # geography / route words
        "nahal",
        "wadi",
        "mount",
        "mt",
        "har",
        "trail",
        "summit",
        "loop",
        "reserve",
        "nature",
        "park",
        "the",
        "of",
        "to",
        "from",
        "and",
        "circuit",
        "path",
        "national",
        "stream",
        "route",
        "road",
        "spring",
        "ein",
        "waterfall",
        "view",
        "viewpoint",
        "lookout",
        "forest",
        "water",
        "hike",
        "walk",
        "נחל",
        "שביל",
        "מסלול",
        "הר",
        "פסגת",
        "שמורת",
        "גן",
        "לאומי",
        "מעגלי",
        "שמורה",
        "דרך",
        "עד",
        "מעיין",
        "עין",
        "מפל",
        "תצפית",
        "מצפה",
        "יער",
        "חורש",
        "מים",
        "נוף",
        "טיול",
        "הליכה",
        "לאורך",
        # trail-marking colours (Israeli trails are named by their marking)
        "red",
        "blue",
        "green",
        "black",
        "orange",
        "purple",
        "white",
        "אדום",
        "כחול",
        "ירוק",
        "שחור",
        "כתום",
        "סגול",
        "לבן",
    }
)
# One-letter Hebrew prefixes: ב (in), ל (to), ה (the), ו (and), מ (from), ש (that), כ (as).
HEBREW_PREFIXES = "בלהומשכ"
TOKEN = re.compile(r"[\w֐-׿]+")


def _variants(token: str) -> set[str]:
    out = {token}
    if len(token) > 3 and token[0] in HEBREW_PREFIXES and "֐" <= token[0] <= "׿":
        out.add(token[1:])
        if len(token) > 4 and token[1] in HEBREW_PREFIXES:
            out.add(token[2:])  # e.g. "ובנחל" -> "נחל"
    return out


def core_tokens(name: str) -> set[str]:
    """Lower-cased distinctive tokens of a trail name (generic words dropped, also after prefixes)."""
    tokens = TOKEN.findall(name.lower())
    return {t for t in tokens if len(t) > 1 and not (_variants(t) & GENERIC_WORDS)}


def text_tokens(text: str) -> set[str]:
    """All tokens of free text plus their Hebrew prefix-stripped variants."""
    out: set[str] = set()
    for t in TOKEN.findall(text.lower()):
        out |= _variants(t)
    return out


def name_match_score(name: str, text: str) -> float:
    """How strongly free text refers to a trail name, 0..1.

    A match needs either every distinctive token of the name in the text, or at least one
    distinctive token of 3+ letters ("כזיב" in "נחל כזיב - שביל אדום"). Hebrew one-letter
    prefixes are stripped on both sides. Score = share of the name's distinctive tokens found.
    """
    core = core_tokens(name)
    if not core:
        return 0.0
    words = text_tokens(text)
    matched = {t for t in core if _variants(t) & words}
    if matched == core or any(len(t) >= 3 for t in matched):
        return len(matched) / len(core)
    return 0.0


def name_in_text(name: str, text: str) -> bool:
    return name_match_score(name, text) > 0
