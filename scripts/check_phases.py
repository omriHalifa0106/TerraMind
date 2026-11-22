"""Check the exit criteria of MVP phases 0 and 1 (see docs/ARCHITECTURE.md, section 6).

    uv run python -m scripts.check_phases            # all checks
    uv run python -m scripts.check_phases --no-llm   # skip the LLM call (no tokens spent)

Prints one line per check (PASS / FAIL / SKIP) and exits non-zero if anything failed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections.abc import Awaitable, Callable

from terramind.config import get_settings
from terramind.db import DatabaseSetupError, connect

Result = tuple[str, str]  # (PASS | FAIL | SKIP, detail)
KZIV = (33.04, 35.22)  # (lat, lon) near Nahal Kziv, Upper Galilee


async def check_db() -> Result:
    try:
        conn = await connect(get_settings().database_url, connect_timeout=5)
    except DatabaseSetupError as exc:
        return "FAIL", str(exc)
    await conn.close()
    return "PASS", get_settings().database_url.rsplit("@", 1)[-1]


async def check_extensions() -> Result:
    conn = await connect(get_settings().database_url)
    try:
        found = {r["extname"] for r in await conn.fetch("SELECT extname FROM pg_extension")}
    finally:
        await conn.close()
    missing = {"postgis", "vector", "pg_trgm"} - found
    return ("FAIL", f"missing: {sorted(missing)}") if missing else ("PASS", "postgis, vector, pg_trgm")


async def check_migrations() -> Result:
    conn = await connect(get_settings().database_url)
    try:
        rows = await conn.fetch("SELECT name FROM schema_migrations ORDER BY name")
    except Exception:
        return "FAIL", "schema_migrations missing: run `uv run python -m scripts.migrate`"
    finally:
        await conn.close()
    names = [r["name"] for r in rows]
    return ("PASS", ", ".join(names)) if len(names) >= 2 else ("FAIL", f"applied: {names}")


async def check_counts() -> Result:
    conn = await connect(get_settings().database_url)
    try:
        r = await conn.fetchrow(
            "SELECT (SELECT count(*) FROM trails) AS trails, (SELECT count(*) FROM pois) AS pois, "
            "(SELECT count(*) FROM knowledge_chunks) AS chunks, "
            "(SELECT count(*) FROM knowledge_chunks WHERE trail_id IS NOT NULL) AS linked, "
            "(SELECT count(*) FROM trails WHERE difficulty IS NULL) AS underived"
        )
    finally:
        await conn.close()
    detail = (
        f"trails={r['trails']} pois={r['pois']} chunks={r['chunks']} "
        f"linked_chunks={r['linked']} trails_without_attributes={r['underived']}"
    )
    if r["trails"] == 0:
        return "FAIL", detail + " -> run ingestion.osm_loader --refresh"
    if r["underived"]:
        return "FAIL", detail + " -> run ingestion.derive_attributes"
    if r["chunks"] == 0:
        return "FAIL", detail + " -> run ingestion.seed_knowledge"
    return "PASS", detail


async def check_spatial() -> Result:
    conn = await connect(get_settings().database_url)
    try:
        rows = await conn.fetch(
            "SELECT name, round((ST_Distance(trailhead::geography, ST_SetSRID(ST_MakePoint($2, $1), 4326)::geography)"
            " / 1000)::numeric, 1) AS km FROM trails "
            "WHERE 'water' = ANY(tags) AND ST_DWithin(trailhead::geography, "
            "ST_SetSRID(ST_MakePoint($2, $1), 4326)::geography, 20000) ORDER BY km LIMIT 3",
            *KZIV,
        )
    finally:
        await conn.close()
    if not rows:
        return "FAIL", "no water trails within 20 km of Nahal Kziv"
    return "PASS", "; ".join(f"{r['name']} ({r['km']} km)" for r in rows)


async def check_semantic() -> Result:
    from mcp_servers.common.db import close_pool, get_pool
    from mcp_servers.knowledge_server.hybrid import hybrid_search
    from terramind.embeddings import build_embedder

    s = get_settings()
    started = time.perf_counter()
    try:
        chunks = await hybrid_search(
            await get_pool(), build_embedder(s), "מסלול מים מוצל למשפחות", None, None, 3
        )
    finally:
        await close_pool()
    if not chunks:
        return "FAIL", "no chunks returned"
    took = time.perf_counter() - started
    top = chunks[0].text[:60].replace("\n", " ")
    return "PASS", f"{len(chunks)} chunks in {took:.1f}s ({s.embedding_backend}); top: {top}..."


async def check_mcp_gateway() -> Result:
    from evals.harness import inprocess_gateway

    async with inprocess_gateway() as gw:
        found = await gw.call("gis", "find_trails_by_name", {"text": "אני רוצה לטייל בנחל כזיב מחר"})
        closed_ok = await gw.call("alerts", "list_active_alerts", {"region": "Upper Galilee"})
    names = [t["name"] for t in found.get("trails", [])]
    if found.get("status") != "ok":
        return "FAIL", f"find_trails_by_name: {found.get('status')} {found.get('detail') or ''}"
    return "PASS", f"4 MCP servers in-process; 'נחל כזיב' -> {names[:2]}; alerts={closed_ok.get('status')}"


async def check_llm() -> Result:
    from langchain_core.messages import HumanMessage
    from pydantic import BaseModel

    from terramind.llm.client import ClaudeLLM, LLMError
    from terramind.llm.router import model_id_for

    s = get_settings()
    if not s.llm_key:
        return "FAIL", "no API key (ANTHROPIC_AUTH_TOKEN or OPENROUTER_API_KEY)"

    class Ping(BaseModel):
        region: str

    try:
        out = await ClaudeLLM().structured(
            "intake", Ping, [HumanMessage("Region of 'מסלול מים בגליל העליון'?")]
        )
    except LLMError as exc:
        return "FAIL", str(exc)[:200]
    return "PASS", f"{s.resolved_llm_provider} / {model_id_for('intake')} -> {out.region}"


PHASES: list[tuple[str, list[tuple[str, Callable[[], Awaitable[Result]]]]]] = [
    (
        "Phase 0 - scaffold",
        [
            ("database reachable", check_db),
            ("extensions installed", check_extensions),
            ("migrations applied", check_migrations),
            ("LLM structured output", check_llm),
        ],
    ),
    (
        "Phase 1 - data",
        [
            ("rows loaded", check_counts),
            ("spatial query: water trails < 20 km", check_spatial),
            ("Hebrew semantic query", check_semantic),
            ("MCP tools on real data", check_mcp_gateway),
        ],
    ),
]


async def main(skip_llm: bool) -> int:
    failed = 0
    blocked = False
    for phase, checks in PHASES:
        print(f"\n{phase}")
        for name, fn in checks:
            if (blocked and fn is not check_llm) or (skip_llm and fn is check_llm):
                status, detail = "SKIP", "--no-llm" if fn is check_llm else "database unreachable"
            else:
                try:
                    status, detail = await fn()
                except Exception as exc:  # report, keep checking the rest
                    status, detail = "FAIL", f"{type(exc).__name__}: {exc}"[:300]
            mark = {"PASS": "✓", "FAIL": "✗", "SKIP": "-"}[status]
            print(f"  {mark} {status:<4}  {name:<38} {detail}")
            failed += status == "FAIL"
            if fn is check_db and status == "FAIL":
                blocked = True
    print("\nAll exit criteria met." if not failed else f"\n{failed} check(s) failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-llm", action="store_true", help="skip the LLM call")
    args = ap.parse_args()
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.exit(asyncio.run(main(args.no_llm), loop_factory=asyncio.SelectorEventLoop))
