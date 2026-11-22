# TerraMind: notes for Claude Code sessions

Course-project submission (due ~2026-12-06): a 5-agent hiking planner. LangGraph + FastMCP +
PostGIS/pgvector + Claude (Anthropic API). Python backend, TypeScript frontend in `web/`.
The code must match the user's spec exactly: 5 agents in a chain (1 request analysis,
2 catalog search: PostGIS + RAG in parallel via tool calling, 3 enrichment: weather/alerts/closures,
unsafe trails vetoed BEFORE planning, 4 route planning: stop order + drive/walk times,
5 presentation), data pipelines that ingest and store weather/alerts, LangSmith, Leaflet UI.
Anything not in the spec is removed, not kept "just in case".

## Status (2026-11-22)

- Steps 1-2 done: cleanup; graph rebuilt as exactly 5 agents in `src/terramind/graph/nodes/`
  (request_analyst, catalog_search, enrichment, route_planner, presenter). No checkpointer or memory:
  one graph run per message. Verified with real Claude: ~25 s per request (was ~110 s).
- Next: 3 data pipeline (scheduled ingestion of weather/alerts into tables), 4 OSRM drive times
  (if time allows), 5 LangSmith check + Hebrew README explaining every part.
- Alerts/closures still come from the Mock Alerts API (matched by trail name) until a real source is found.
- `docs/ARCHITECTURE.md` describes the old 12-node design; it is replaced by the README in step 5.

## Rules

- Python 3.12+, full type hints; `mypy --strict` and `ruff` must pass. TS: `strict`, `npm run typecheck`.
- Async end to end. CPU-bound work (embeddings) via `asyncio.to_thread`.
- Every external call: explicit timeout, bounded retries, typed failure result. MCP tools return a
  `status` field and never raise into the agent.
- Comments and docstrings in English only. User-facing explanations to the user: Hebrew.
- Config only via `pydantic-settings` / `.env`. No secrets or model IDs in code.
- Agents never touch the DB or HTTP directly: all data goes through the MCP servers (`McpGateway`).
- `terramind.schemas` are frozen Pydantic models and must not import LangGraph.
- Tests never call the real LLM: inject `tests/fakes.py:FakeLLM` into `rt.context.llm`.
- Safety vetoes are decided by `safety/rules.py` (deterministic), never by the LLM.
- Before using a LangGraph / FastMCP / langchain API, check the installed version's signature.
- Keep `tests/` slim: safety rules, routing, MCP servers, one graph run. `tests/unit` has no DB/network.

## Commands

```
uv sync --extra embeddings                 # deps (+ torch for bge-m3)
docker compose up -d db                    # database (host port 5433)
uv run python -m scripts.migrate           # schema
uv run python -m ingestion.osm_loader      # then derive_attributes, seed_knowledge
uv run python -m scripts.dev               # backend stack -> http://127.0.0.1:8000 (serves web/dist)
cd web; npm run dev                        # frontend dev server -> http://localhost:5174 (proxies API)
cd web; npm run build                      # production build into web/dist
uv run pytest                              # TERRAMIND_TEST_DATABASE_URL=...5433/terramind_test for DB tests
uv run ruff check . && uv run mypy src mcp_servers services ingestion scripts
```
