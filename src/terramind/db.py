"""Database connection for scripts, with actionable error messages for common setup mistakes."""

from __future__ import annotations

from urllib.parse import urlsplit

import asyncpg


class DatabaseSetupError(RuntimeError):
    pass


def _where(dsn: str) -> str:
    parts = urlsplit(dsn)
    return f"{parts.hostname}:{parts.port or 5432} as {parts.username!r}"


async def connect(dsn: str, connect_timeout: float = 10) -> asyncpg.Connection:
    try:
        conn: asyncpg.Connection = await asyncpg.connect(dsn, timeout=connect_timeout)
    except asyncpg.InvalidPasswordError as exc:
        raise DatabaseSetupError(
            f"Password rejected at {_where(dsn)}. Usually this means a DIFFERENT PostgreSQL is listening "
            "on that port (a local Windows install or another project's container), not TerraMind's. "
            "TerraMind's container listens on 5433: check DATABASE_URL in .env, and run "
            "`docker compose ps` to see that the `db` service is up."
        ) from exc
    except asyncpg.InvalidCatalogNameError as exc:
        raise DatabaseSetupError(f"Database missing at {_where(dsn)}: {exc}") from exc
    except (OSError, TimeoutError) as exc:
        raise DatabaseSetupError(
            f"Cannot reach PostgreSQL at {_where(dsn)} ({type(exc).__name__}). "
            "Start it with `docker compose up -d db` and wait until `docker compose ps` shows it healthy."
        ) from exc
    return conn
