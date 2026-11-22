"""Text embedders shared by ingestion and the knowledge MCP server.

`BgeEmbedder` is the production backend (BAAI/bge-m3, multilingual, 1024 dims).
`HashEmbedder` is a deterministic, dependency-free fallback for tests and offline demos:
it hashes character n-grams, so it captures lexical overlap only (no semantics).
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import threading
import unicodedata
from typing import Any, Protocol

from terramind.config import Settings, get_settings


class Embedder(Protocol):
    dim: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def to_pgvector(vec: list[float]) -> str:
    """Serialize a vector to pgvector's text input format."""
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    # Strip Hebrew niqqud and punctuation; keep letters/digits of any script.
    text = re.sub(r"[֑-ׇ]", "", text)
    return re.sub(r"[^\w\s]", " ", text)


class HashEmbedder:
    def __init__(self, dim: int = 1024, n: int = 3) -> None:
        self.dim = dim
        self._n = n

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in _normalize(text).split():
            padded = f" {token} "
            grams = [padded[i : i + self._n] for i in range(max(1, len(padded) - self._n + 1))]
            for gram in [token, *grams]:
                digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
                idx = int.from_bytes(digest[:4], "little") % self.dim
                sign = 1.0 if digest[4] & 1 else -1.0
                vec[idx] += sign
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]


class BgeEmbedder:
    """Lazy-loads sentence-transformers so importing this module stays cheap."""

    def __init__(self, model_name: str = "BAAI/bge-m3", dim: int = 1024) -> None:
        self.dim = dim
        self._model_name = model_name
        self._model: Any = None
        self._loading: asyncio.Future[Any] | None = None
        self._thread_lock = threading.Lock()

    def _load_sync(self) -> Any:
        with self._thread_lock:
            if self._model is None:
                try:
                    from sentence_transformers import SentenceTransformer
                except ImportError as exc:  # pragma: no cover - depends on optional extra
                    raise RuntimeError(
                        "sentence-transformers is not installed. Run `uv sync --extra embeddings` "
                        "or set EMBEDDING_BACKEND=hash for an offline demo."
                    ) from exc
                self._model = SentenceTransformer(self._model_name)
            return self._model

    async def _load(self) -> Any:
        """Load once. The load runs as one shared task that a caller's timeout cannot cancel:
        a cancelled `await to_thread(...)` would finish loading in the thread but drop the
        result, so every timed-out call would start the (slow) load over again."""
        if self._model is not None:
            return self._model
        if self._loading is None or (self._loading.done() and self._loading.exception() is not None):
            self._loading = asyncio.ensure_future(asyncio.to_thread(self._load_sync))
        return await asyncio.shield(self._loading)

    async def warm_up(self) -> None:
        await self.embed(["warm-up"])

    async def embed(self, texts: list[str]) -> list[list[float]]:
        model = await self._load()

        def _encode() -> list[list[float]]:
            arr = model.encode(texts, normalize_embeddings=True, batch_size=16, show_progress_bar=False)
            return [list(map(float, row)) for row in arr]

        # Encoding is CPU/GPU bound: keep it off the event loop.
        return await asyncio.to_thread(_encode)


def build_embedder(settings: Settings | None = None) -> Embedder:
    s = settings or get_settings()
    if s.embedding_backend == "hash":
        return HashEmbedder(dim=s.embedding_dim)
    if s.embedding_backend == "bge":
        return BgeEmbedder(model_name=s.embedding_model, dim=s.embedding_dim)
    raise ValueError(f"Unknown EMBEDDING_BACKEND: {s.embedding_backend!r}")
