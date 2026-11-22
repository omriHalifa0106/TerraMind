"""Lazily created asyncpg pool shared by the tools of one MCP server process."""

from __future__ import annotations

import asyncio

import asyncpg

from terramind.config import get_settings

_pool: asyncpg.Pool | None = None
_lock = asyncio.Lock()


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        async with _lock:
            if _pool is None:
                s = get_settings()
                _pool = await asyncpg.create_pool(
                    s.database_url,
                    min_size=s.db_pool_min,
                    max_size=s.db_pool_max,
                    timeout=10,
                    command_timeout=s.db_statement_timeout_ms / 1000,
                    # Server-side guard as well, so a runaway spatial query cannot hang a tool.
                    server_settings={"statement_timeout": str(s.db_statement_timeout_ms)},
                )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
