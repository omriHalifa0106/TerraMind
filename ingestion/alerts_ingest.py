"""Alerts pipeline: trail status (open/closed, crowding) and active alerts, stored in PostGIS.

Source: the Mock Alerts API (until a real feed is connected). For every trail it asks for the
status and the trail-specific alerts; region-wide alerts are fetched once. The tables are then
replaced in one transaction, for the trails that answered, so a reader never sees half a run.
Trails whose requests failed keep their previous rows (and the run is marked 'partial').

    uv run python -m ingestion.alerts_ingest
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Any

import httpx
from pydantic import ValidationError

from ingestion.runs import IngestionFailed, Trigger, recorded_run
from mcp_servers.common.http import build_client
from terramind.config import get_settings
from terramind.db import connect
from terramind.schemas import TrailAlert, TrailStatus

log = logging.getLogger("terramind.ingestion.alerts")

LOCK_KEY = 74_201  # pg advisory lock: one alerts run at a time (schedule vs. scenario switch)

INSERT_ALERT = """
INSERT INTO trail_alerts (alert_id, trail_id, region, kind, severity, message, valid_from, valid_to, run_id)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
ON CONFLICT (alert_id, COALESCE(trail_id, '')) DO NOTHING
"""
UPSERT_STATUS = """
INSERT INTO trail_status (trail_id, is_open, reason, crowd_level, fetched_at, run_id)
VALUES ($1, $2, $3, $4, now(), $5)
ON CONFLICT (trail_id) DO UPDATE SET
    is_open = EXCLUDED.is_open, reason = EXCLUDED.reason, crowd_level = EXCLUDED.crowd_level,
    fetched_at = now(), run_id = EXCLUDED.run_id
"""


async def _get(client: httpx.AsyncClient, path: str, params: dict[str, str]) -> Any:
    resp = await client.get(path, params=params)
    resp.raise_for_status()
    return resp.json()


async def _trail(
    client: httpx.AsyncClient, sem: asyncio.Semaphore, trail_id: str, names: str, on: date
) -> tuple[TrailStatus, list[TrailAlert]]:
    async with sem:
        status_raw, alerts_raw = await asyncio.gather(
            _get(client, "/trails/status", {"trail_id": trail_id, "name": names, "on": on.isoformat()}),
            _get(client, "/alerts", {"trail_id": trail_id, "name": names, "on": on.isoformat()}),
        )
    status = TrailStatus.model_validate(status_raw)
    # Region-wide alerts come back here too (trail_id null); they are stored once, separately.
    alerts = [a for a in (TrailAlert.model_validate(x) for x in alerts_raw) if a.trail_id]
    return status, alerts


async def run(
    dsn: str | None = None, trigger: Trigger = "manual", client: httpx.AsyncClient | None = None
) -> int:
    """One pipeline run. Returns the number of status + alert rows written."""
    s = get_settings()
    conn = await connect(dsn or s.database_url)
    own_client = client is None
    http = client or build_client(s.mock_alerts_url)
    on = date.today()
    try:
        async with recorded_run(conn, "alerts", trigger) as run_log:
            trails = await conn.fetch("SELECT trail_id, name, name_he FROM trails ORDER BY trail_id")
            if not trails:
                raise IngestionFailed("no trails in the database: run the OSM loader first")
            sem = asyncio.Semaphore(max(1, s.ingest_concurrency))
            # Same "name / name_he" text that agent 3 sends, so name matching behaves the same.
            names = {
                t["trail_id"]: " / ".join(dict.fromkeys(n for n in (t["name"], t["name_he"]) if n))
                for t in trails
            }
            results = await asyncio.gather(
                *(_trail(http, sem, tid, names[tid], on) for tid in names), return_exceptions=True
            )
            try:
                region_wide = [
                    TrailAlert.model_validate(x) for x in await _get(http, "/alerts", {"on": on.isoformat()})
                ]
            except (httpx.HTTPError, ValidationError, ValueError) as exc:
                region_wide = None  # keep the stored region-wide alerts
                run_log.failures.append(f"region alerts: {type(exc).__name__}")

            ok: dict[str, tuple[TrailStatus, list[TrailAlert]]] = {}
            for tid, res in zip(names, results, strict=True):
                if isinstance(res, BaseException):
                    if not isinstance(res, httpx.HTTPError | ValidationError | ValueError):
                        raise res
                    run_log.failures.append(f"{tid}: {type(res).__name__}")
                else:
                    ok[tid] = res
            if not ok and region_wide is None:
                raise IngestionFailed(run_log.detail() or "alerts source returned nothing")

            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock($1)", LOCK_KEY)
                fresh = list(ok)
                await conn.execute("DELETE FROM trail_alerts WHERE trail_id = ANY($1::text[])", fresh)
                if region_wide is not None:
                    await conn.execute("DELETE FROM trail_alerts WHERE trail_id IS NULL")
                alerts = [a for _, trail_alerts in ok.values() for a in trail_alerts] + (region_wide or [])
                await conn.executemany(
                    INSERT_ALERT,
                    [
                        (
                            a.alert_id,
                            a.trail_id,
                            a.region,
                            a.kind,
                            a.severity,
                            a.message,
                            a.valid_from,
                            a.valid_to,
                            run_log.run_id,
                        )
                        for a in alerts
                    ],
                )
                await conn.executemany(
                    UPSERT_STATUS,
                    [
                        (st.trail_id, st.is_open, st.reason, st.crowd_level, run_log.run_id)
                        for st, _ in ok.values()
                    ],
                )
            run_log.rows = len(ok) + len(alerts)
            log.info("alerts: %d trails, %d alerts (%s)", len(ok), len(alerts), run_log.status)
            return run_log.rows
    finally:
        if own_client:
            await http.aclose()
        await conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    print("rows written:", asyncio.run(run()))
