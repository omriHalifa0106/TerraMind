"""Embed curated trail descriptions and tips into pgvector, then link each chunk to a trail.

Chunks are keyed by a hash of their text, so re-running is idempotent. Linking matches the core
tokens of `trail_name_hint` (generic words such as "Nahal"/"נחל" removed) against the
trail's Hebrew, English and OSM names; ambiguous matches are left unlinked.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import asyncpg

from terramind.config import get_settings
from terramind.db import DatabaseSetupError, connect
from terramind.embeddings import Embedder, build_embedder, to_pgvector
from terramind.text import core_tokens

DEFAULT_FILE = Path(__file__).resolve().parent / "data" / "tips.jsonl"
LINK_MIN_OVERLAP = 1.0  # every core token of the hint must appear in the trail name


@dataclass(frozen=True)
class Tip:
    text: str
    lang: str
    source: str
    trail_name_hint: str | None

    @property
    def chunk_id(self) -> str:
        return "kc-" + hashlib.sha1(self.text.encode("utf-8")).hexdigest()[:16]


def read_tips(path: Path) -> list[Tip]:
    tips: list[Tip] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        raw = json.loads(line)
        if raw.get("lang") not in ("he", "en") or not raw.get("text"):
            raise ValueError(f"{path.name}:{line_no}: needs lang in (he, en) and non-empty text")
        tips.append(
            Tip(raw["text"].strip(), raw["lang"], raw.get("source", "unknown"), raw.get("trail_name_hint"))
        )
    return tips


UPSERT = """
INSERT INTO knowledge_chunks (chunk_id, trail_id, trail_name_hint, region, lang, source, text, embedding)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8::vector)
ON CONFLICT (chunk_id) DO UPDATE SET
    trail_id = EXCLUDED.trail_id, trail_name_hint = EXCLUDED.trail_name_hint,
    source = EXCLUDED.source, embedding = EXCLUDED.embedding
"""


def match_trail(hint: str, trails: list[tuple[str, list[str]]]) -> str | None:
    """Return the trail whose names share the most core tokens with the hint (ties -> None)."""
    hint_tokens = core_tokens(hint)
    if not hint_tokens:
        return None
    scored: list[tuple[float, str]] = []
    for trail_id, names in trails:
        best = 0.0
        for name in names:
            tokens = core_tokens(name)
            if tokens:
                best = max(best, len(hint_tokens & tokens) / len(hint_tokens))
        if best >= LINK_MIN_OVERLAP:
            scored.append((best, trail_id))
    scored.sort(reverse=True)
    if not scored or (len(scored) > 1 and scored[0][0] == scored[1][0]):
        return None  # no match, or ambiguous: leave unlinked rather than guess
    return scored[0][1]


async def seed(conn: asyncpg.Connection, tips: list[Tip], embedder: Embedder, region: str) -> tuple[int, int]:
    vectors = await embedder.embed([t.text for t in tips])
    rows = await conn.fetch("SELECT trail_id, name, name_he, name_en FROM trails")
    trails = [(r["trail_id"], [n for n in (r["name"], r["name_he"], r["name_en"]) if n]) for r in rows]
    links = {t.chunk_id: match_trail(t.trail_name_hint, trails) if t.trail_name_hint else None for t in tips}
    async with conn.transaction():
        await conn.executemany(
            UPSERT,
            [
                (
                    t.chunk_id,
                    links[t.chunk_id],
                    t.trail_name_hint,
                    region,
                    t.lang,
                    t.source,
                    t.text,
                    to_pgvector(v),
                )
                for t, v in zip(tips, vectors, strict=True)
            ],
        )
    return len(tips), sum(1 for v in links.values() if v)


async def run(path: Path, dsn: str | None = None) -> tuple[int, int]:
    settings = get_settings()
    conn = await connect(dsn or settings.database_url)
    try:
        return await seed(conn, read_tips(path), build_embedder(settings), settings.region_name)
    finally:
        await conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", type=Path, default=DEFAULT_FILE)
    args = ap.parse_args()
    n, linked = asyncio.run(run(args.file))
    print(f"chunks upserted={n} linked_to_trails={linked}")


def cli() -> None:
    try:
        main()
    except (DatabaseSetupError, RuntimeError) as exc:
        raise SystemExit(f"ERROR: {exc}") from None


if __name__ == "__main__":
    cli()
