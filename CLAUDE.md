# TerraMind: notes for Claude Code sessions

Multi-agent hiking planner: LangGraph + FastMCP + PostGIS/pgvector + Claude via OpenRouter.
Design: `docs/ARCHITECTURE.md`. Deviations from it: README "Implementation notes".

## Status

MVP phases 0-9 are implemented and tested (offline: HeuristicLLM, synthetic OSM fixture).
Verified on this machine (2026-11-19..21): real Overpass ingestion, bge-m3, real Claude calls.
`evals.run_evals --judge` with Claude on 2026-11-21: all 8 scenarios pass, faithfulness 0.88-1.0.
Not yet verified: LangSmith experiment upload / prompt-version comparison (phase 9 exit).
Open issues: latency 50-160 s per request; only 8 eval scenarios (plan: ~30); 25 tips (plan: ~60).
Next: expand `scenarios.yaml`, profile and cut latency, LangSmith prompt A/B report.
Stretch ideas: live re-planning from GPS, multi-agent negotiation, digital twin from `pace_kmh`.

## Rules

- Python 3.12+, full type hints; `mypy --strict` and `ruff` must pass.
- Async end to end. CPU-bound work (embeddings) via `asyncio.to_thread`.
- Every external call: explicit timeout, bounded retries, typed failure result. MCP tools return a
  `status` field and never raise into the agent.
- Comments and docstrings in English only.
- Config only via `pydantic-settings` / `.env`. No secrets or model IDs in code.
- Agents never touch the DB or HTTP directly: all data goes through the MCP servers (`ToolGateway`).
- `terramind.schemas` are frozen Pydantic models and must not import LangGraph. New state models
  must be importable from `terramind.schemas` so the checkpoint serializer registers them.
- Safety vetoes are decided by `safety/rules.py`. The LLM critic may only add warnings.
- Before using a LangGraph / FastMCP / langchain API, check the installed version's signature.
- Prefer a failing test first for bugs; keep `tests/unit` free of DB and network.

## Commands

```
uv sync --extra embeddings                 # deps (+ torch for bge-m3)
docker compose up -d db                    # database
uv run python -m scripts.migrate           # schema
uv run python -m ingestion.osm_loader      # then derive_attributes, seed_knowledge
uv run python -m scripts.dev               # whole stack -> http://127.0.0.1:8000
uv run python -m scripts.demo_cli "..." --scenario kziv_closed
uv run pytest                              # set TERRAMIND_TEST_DATABASE_URL for DB tests
uv run ruff check . && uv run mypy src mcp_servers services ingestion evals scripts
uv run python -m evals.run_evals [--judge] [--langsmith]
```
