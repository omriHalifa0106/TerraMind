"""knowledge-server: semantic + full-text search over trail descriptions and tips (pgvector)."""

from __future__ import annotations

import logging
from typing import Literal

import asyncpg
from fastmcp import FastMCP

from mcp_servers.common.db import get_pool
from mcp_servers.knowledge_server import hybrid
from terramind.config import get_settings
from terramind.embeddings import Embedder, build_embedder
from terramind.schemas import EvidenceChunk
from terramind.schemas.tool_results import EvidenceResult

log = logging.getLogger("terramind.mcp.knowledge")
mcp = FastMCP("terramind-knowledge", instructions="Search trail descriptions, hiker tips and safety notes.")

_embedder: Embedder | None = None
ERRORS = (asyncpg.PostgresError, OSError, TimeoutError, RuntimeError)


def get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        _embedder = build_embedder()
    return _embedder


def _result(chunks: list[EvidenceChunk]) -> EvidenceResult:
    return EvidenceResult(status="ok" if chunks else "empty", chunks=chunks)


@mcp.tool
async def hybrid_search(
    query: str, trail_ids: list[str] | None = None, lang: Literal["he", "en"] | None = None, k: int = 8
) -> EvidenceResult:
    """Best retrieval: vector similarity and full-text matches fused with Reciprocal Rank Fusion.
    With trail_ids, returns chunks about those trails plus general (unlinked) safety tips."""
    try:
        chunks = await hybrid.hybrid_search(
            await get_pool(), get_embedder(), query, trail_ids, lang, min(k, 30)
        )
    except ERRORS as exc:
        log.exception("hybrid_search failed")
        return EvidenceResult(status="error", detail=f"{type(exc).__name__}: {exc}"[:300])
    return _result(chunks)


@mcp.tool
async def get_trail_notes(trail_id: str, k: int = 6) -> EvidenceResult:
    """All stored descriptions and tips linked to one trail."""
    try:
        rows = await (await get_pool()).fetch(
            "SELECT chunk_id, trail_id, text, source, 1.0 AS score FROM knowledge_chunks "
            "WHERE trail_id = $1 ORDER BY lang, chunk_id LIMIT $2",
            trail_id,
            min(k, 30),
        )
    except ERRORS as exc:
        return EvidenceResult(status="error", detail=f"{type(exc).__name__}"[:300])
    return _result(hybrid._rows_to_chunks(rows))


async def warm_up() -> None:
    """Load the embedding model before serving, so the first query does not hit the tool timeout."""
    await get_embedder().embed(["warm-up"])


if __name__ == "__main__":
    import asyncio

    logging.basicConfig(level=logging.INFO)
    log.info("loading embedding model (first run downloads BAAI/bge-m3, ~2 GB)...")
    asyncio.run(warm_up())
    mcp.run(transport="http", host=get_settings().mcp_bind_host, port=8102)
