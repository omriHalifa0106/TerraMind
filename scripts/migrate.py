"""Apply plain-SQL migrations in db/migrations/ in order, exactly once each."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from terramind.config import get_settings
from terramind.db import DatabaseSetupError, connect

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"


async def migrate(dsn: str) -> list[str]:
    conn = await connect(dsn)
    applied: list[str] = []
    try:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        done = {r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            async with conn.transaction():
                await conn.execute(path.read_text(encoding="utf-8"))
                await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", path.name)
            applied.append(path.name)
    finally:
        await conn.close()
    return applied


def main() -> None:
    dsn = sys.argv[1] if len(sys.argv) > 1 else get_settings().database_url
    applied = asyncio.run(migrate(dsn))
    print("applied:", applied or "nothing (up to date)")


def cli() -> None:
    try:
        main()
    except (DatabaseSetupError, RuntimeError) as exc:
        raise SystemExit(f"ERROR: {exc}") from None


if __name__ == "__main__":
    cli()
