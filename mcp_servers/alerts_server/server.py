"""alerts-server: read-only access to trail status, closures and alerts over MCP.

Reads what the data pipeline stored (ingestion/alerts_ingest.py: trail_status, trail_alerts).
If the last successful alerts run is older than ALERTS_MAX_AGE_MIN, or a trail has no stored
status, the answer is `unavailable`: the safety rules turn that into a warning, never into "safe".
"""

from __future__ import annotations

import logging
from datetime import date, datetime

import asyncpg
from fastmcp import FastMCP

from mcp_servers.common.db import get_pool
from terramind.config import get_settings
from terramind.regions import resolve_region
from terramind.schemas import TrailAlert, TrailStatus
from terramind.schemas.tool_results import AlertsResult, TrailStatusResult

log = logging.getLogger("terramind.mcp.alerts")
mcp = FastMCP("terramind-alerts", instructions="Trail closures, crowding and safety alerts. Read-only.")

DB_ERRORS = (asyncpg.PostgresError, OSError, TimeoutError)

LAST_GOOD_RUN = """
SELECT max(finished_at) FROM ingestion_runs WHERE source = 'alerts' AND status IN ('ok', 'partial')
"""

ALERTS = """
SELECT alert_id, trail_id, region, kind, severity, message, valid_from, valid_to
FROM trail_alerts
WHERE (trail_id = $1 OR (trail_id IS NULL AND ($2::text IS NULL OR region IS NULL OR region = $2)))
  AND valid_from::date <= $3 AND (valid_to IS NULL OR valid_to::date >= $3)
ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, alert_id
"""


async def _stale_reason(conn: asyncpg.Pool) -> str | None:
    """Why the stored data cannot be trusted right now (None = fresh enough)."""
    last: datetime | None = await conn.fetchval(LAST_GOOD_RUN)
    if last is None:
        return "no alerts data ingested yet (is the pipeline running?)"
    age_min = (datetime.now(last.tzinfo) - last).total_seconds() / 60
    if age_min > get_settings().alerts_max_age_min:
        return f"alerts data is {age_min:.0f} min old"
    return None


@mcp.tool
async def get_trail_status(
    trail_id: str, trail_name: str | None = None, for_date: date | None = None
) -> TrailStatusResult:
    """Whether a trail is open on a date, why not, and how crowded it is expected to be."""
    unknown = TrailStatus(trail_id=trail_id, is_open=True, status="unavailable")
    try:
        pool = await get_pool()
        if (why := await _stale_reason(pool)) is not None:
            return TrailStatusResult(status="unavailable", detail=why, trail=unknown)
        row = await pool.fetchrow(
            "SELECT is_open, reason, crowd_level FROM trail_status WHERE trail_id = $1", trail_id
        )
    except DB_ERRORS as exc:
        log.warning("trail status unavailable: %s", type(exc).__name__)
        return TrailStatusResult(
            status="unavailable", detail=f"database error ({type(exc).__name__})", trail=unknown
        )
    if row is None:
        return TrailStatusResult(
            status="unavailable", detail="no stored status for this trail", trail=unknown
        )
    return TrailStatusResult(
        status="ok",
        trail=TrailStatus(
            trail_id=trail_id, is_open=row["is_open"], reason=row["reason"], crowd_level=row["crowd_level"]
        ),
    )


@mcp.tool
async def list_active_alerts(
    region: str | None = None,
    trail_id: str | None = None,
    trail_name: str | None = None,
    for_date: date | None = None,
) -> AlertsResult:
    """Active alerts (flood, fire, closure, military, maintenance, crowding) for a region and/or trail."""
    wanted = resolve_region(region) if region else None
    try:
        pool = await get_pool()
        if (why := await _stale_reason(pool)) is not None:
            return AlertsResult(status="unavailable", detail=why)
        rows = await pool.fetch(ALERTS, trail_id, wanted.key if wanted else None, for_date or date.today())
    except DB_ERRORS as exc:
        return AlertsResult(status="unavailable", detail=f"database error ({type(exc).__name__})")
    alerts = [TrailAlert.model_validate(dict(r)) for r in rows]
    return AlertsResult(status="ok" if alerts else "empty", alerts=alerts)


if __name__ == "__main__":
    mcp.run(transport="http", host=get_settings().mcp_bind_host, port=8104)
