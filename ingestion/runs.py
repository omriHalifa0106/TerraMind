"""ingestion_runs bookkeeping: every pipeline run leaves one row saying what it did and how it ended."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Literal

import asyncpg

Source = Literal["weather", "alerts"]
Trigger = Literal["schedule", "manual", "scenario", "test"]


@dataclass
class RunLog:
    run_id: int
    rows: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def status(self) -> Literal["ok", "partial"]:
        return "partial" if self.failures else "ok"

    def detail(self) -> str | None:
        if not self.failures:
            return None
        shown = "; ".join(self.failures[:5])
        more = f" (+{len(self.failures) - 5} more)" if len(self.failures) > 5 else ""
        return f"{len(self.failures)} failed: {shown}{more}"[:1000]


class IngestionFailed(RuntimeError):
    """Nothing usable came back from the source; stored data was left untouched."""


@asynccontextmanager
async def recorded_run(conn: asyncpg.Connection, source: Source, trigger: Trigger) -> AsyncIterator[RunLog]:
    """Insert a 'running' row, then close it as ok / partial / failed when the block ends."""
    run_id: int = await conn.fetchval(
        "INSERT INTO ingestion_runs (source, trigger) VALUES ($1, $2) RETURNING run_id", source, trigger
    )
    log = RunLog(run_id)
    try:
        yield log
    except BaseException as exc:
        # Best effort: a broken connection must not hide the original error.
        with contextlib.suppress(asyncpg.PostgresError, OSError):
            await conn.execute(
                "UPDATE ingestion_runs SET status = 'failed', finished_at = now(), detail = $2 "
                "WHERE run_id = $1",
                run_id,
                f"{type(exc).__name__}: {exc}"[:1000],
            )
        raise
    await conn.execute(
        "UPDATE ingestion_runs SET status = $2, finished_at = now(), rows_written = $3, detail = $4 "
        "WHERE run_id = $1",
        run_id,
        log.status,
        log.rows,
        log.detail(),
    )
