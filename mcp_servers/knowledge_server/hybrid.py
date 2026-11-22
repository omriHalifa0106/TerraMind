"""Hybrid retrieval: pgvector cosine search + Postgres full-text search, fused with RRF."""

from __future__ import annotations

import re

import asyncpg

from terramind.embeddings import Embedder, to_pgvector
from terramind.schemas import EvidenceChunk

RRF_K = 60  # standard constant from the Reciprocal Rank Fusion paper
CANDIDATES_PER_LIST = 30

VECTOR_SQL = """
SELECT chunk_id, trail_id, text, source, 1 - (embedding <=> $1::vector) AS score
FROM knowledge_chunks
WHERE ($2::text[] IS NULL OR trail_id = ANY($2::text[]) OR trail_id IS NULL)
  AND ($3::text IS NULL OR lang = $3)
ORDER BY embedding <=> $1::vector
LIMIT $4
"""

FTS_SQL = """
SELECT chunk_id, trail_id, text, source, ts_rank_cd(tsv, q) AS score
FROM knowledge_chunks, to_tsquery('simple', $1) AS q
WHERE tsv @@ q
  AND ($2::text[] IS NULL OR trail_id = ANY($2::text[]) OR trail_id IS NULL)
  AND ($3::text IS NULL OR lang = $3)
ORDER BY score DESC
LIMIT $4
"""


def to_or_tsquery(query: str) -> str | None:
    """'shaded water hike' -> 'shaded | water | hike'.

    OR semantics on purpose: Postgres has no Hebrew stemmer, so AND would be far too strict.
    """
    tokens = [t for t in re.findall(r"[\w֐-׿]+", query.lower()) if len(t) > 1]
    return " | ".join(dict.fromkeys(tokens)) or None


def rrf_fuse(ranked_lists: list[list[EvidenceChunk]], k: int) -> list[EvidenceChunk]:
    scores: dict[str, float] = {}
    first_seen: dict[str, EvidenceChunk] = {}
    for ranked in ranked_lists:
        for rank, chunk in enumerate(ranked, start=1):
            scores[chunk.chunk_id] = scores.get(chunk.chunk_id, 0.0) + 1.0 / (RRF_K + rank)
            first_seen.setdefault(chunk.chunk_id, chunk)
    ordered = sorted(scores, key=lambda cid: scores[cid], reverse=True)[:k]
    return [first_seen[cid].model_copy(update={"score": round(scores[cid], 5)}) for cid in ordered]


def _rows_to_chunks(rows: list[asyncpg.Record]) -> list[EvidenceChunk]:
    return [
        EvidenceChunk(
            chunk_id=r["chunk_id"],
            trail_id=r["trail_id"],
            text=r["text"],
            source=r["source"],
            score=round(float(r["score"]), 5),
        )
        for r in rows
    ]


async def vector_search(
    pool: asyncpg.Pool, embedder: Embedder, query: str, trail_ids: list[str] | None, lang: str | None, k: int
) -> list[EvidenceChunk]:
    [vec] = await embedder.embed([query])
    rows = await pool.fetch(VECTOR_SQL, to_pgvector(vec), trail_ids, lang, k)
    return _rows_to_chunks(rows)


async def fts_search(
    pool: asyncpg.Pool, query: str, trail_ids: list[str] | None, lang: str | None, k: int
) -> list[EvidenceChunk]:
    tsq = to_or_tsquery(query)
    if tsq is None:
        return []
    rows = await pool.fetch(FTS_SQL, tsq, trail_ids, lang, k)
    return _rows_to_chunks(rows)


async def hybrid_search(
    pool: asyncpg.Pool, embedder: Embedder, query: str, trail_ids: list[str] | None, lang: str | None, k: int
) -> list[EvidenceChunk]:
    vec = await vector_search(pool, embedder, query, trail_ids, lang, CANDIDATES_PER_LIST)
    fts = await fts_search(pool, query, trail_ids, lang, CANDIDATES_PER_LIST)
    return rrf_fuse([vec, fts], k)
