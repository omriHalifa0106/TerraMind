"""Data pipeline scheduler: keeps weather forecasts and trail alerts fresh in the database.

    uv run python -m ingestion.pipeline                  # run forever (started by scripts.dev / compose)
    uv run python -m ingestion.pipeline --once           # one run of each job, then exit
    uv run python -m ingestion.pipeline --once alerts    # just one job

Each job runs at start-up and then every N minutes (WEATHER_INGEST_INTERVAL_MIN,
ALERTS_INGEST_INTERVAL_MIN). A failed run is recorded in ingestion_runs and retried after
INGEST_RETRY_AFTER_FAILURE_S instead of waiting a full interval. Jobs never crash the loop.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ingestion import alerts_ingest, weather_ingest
from ingestion.runs import Trigger
from terramind.config import Settings, get_settings

log = logging.getLogger("terramind.pipeline")


@dataclass(frozen=True)
class Job:
    name: str
    run: Callable[..., Awaitable[int]]
    interval_min: Callable[[Settings], float]


JOBS = {
    "weather": Job("weather", weather_ingest.run, lambda s: s.weather_ingest_interval_min),
    "alerts": Job("alerts", alerts_ingest.run, lambda s: s.alerts_ingest_interval_min),
}


async def run_once(job: Job, trigger: Trigger) -> bool:
    """Run a job with a hard time limit. Returns False on failure (already logged and recorded)."""
    s = get_settings()
    try:
        async with asyncio.timeout(s.ingest_job_timeout_s):
            rows = await job.run(s.database_url, trigger)
    except Exception as exc:
        log.error("%s run failed: %s: %s", job.name, type(exc).__name__, exc)
        return False
    log.info("%s run ok: %d rows", job.name, rows)
    return True


async def _forever(job: Job) -> None:
    s = get_settings()
    while True:
        ok = await run_once(job, "schedule")
        await asyncio.sleep(job.interval_min(s) * 60 if ok else s.ingest_retry_after_failure_s)


async def main_async(once: bool, names: list[str]) -> int:
    jobs = [JOBS[n] for n in names]
    if once:
        results = [await run_once(j, "manual") for j in jobs]
        return 0 if all(results) else 1
    log.info(
        "pipeline started: %s",
        ", ".join(f"{j.name} every {j.interval_min(get_settings()):g} min" for j in jobs),
    )
    await asyncio.gather(*(_forever(j) for j in jobs))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--once", action="store_true", help="run each job once and exit")
    parser.add_argument(
        "jobs", nargs="*", metavar="JOB", help=f"jobs to run: {', '.join(JOBS)} (default: all)"
    )
    args = parser.parse_args()
    if unknown := set(args.jobs) - set(JOBS):
        parser.error(f"unknown job(s): {', '.join(sorted(unknown))}")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request is noise here
    try:
        raise SystemExit(asyncio.run(main_async(args.once, args.jobs or list(JOBS))))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
