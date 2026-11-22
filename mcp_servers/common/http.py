"""HTTP client factory with explicit timeouts and connection-level retries."""

from __future__ import annotations

import httpx

from terramind.config import get_settings


def build_client(base_url: str = "") -> httpx.AsyncClient:
    s = get_settings()
    return httpx.AsyncClient(
        base_url=base_url,
        timeout=httpx.Timeout(connect=3.0, read=s.http_timeout_s, write=3.0, pool=3.0),
        transport=httpx.AsyncHTTPTransport(retries=2),
        headers={"User-Agent": "TerraMind/0.1"},
    )
