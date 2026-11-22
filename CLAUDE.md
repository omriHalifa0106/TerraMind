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
  (request_analyst, catalog_search, enrichment, route_planner, presenter). One graph run per message;
  chat memory is the `conversations` table (not a LangGraph checkpointer: psycopg async fails on
  Windows' default Proactor event loop). Verified with real Claude: ~25 s per request (was ~110 s).
- Step 3 done: data pipeline (`ingestion/pipeline.py`, migration 003). Weather (Open-Meteo, batched,
  16 days per trailhead cell) and alerts (Mock Alerts API) are ingested on a schedule into
  `weather_forecasts`, `trail_status`, `trail_alerts`; every run is logged in `ingestion_runs`.
  The weather/alerts MCP servers read these tables (weather falls back to a live fetch; stale alerts
  -> `unavailable`). Switching the demo scenario triggers an alerts run (API `/scenarios/{name}`).
- Step 4 done: OSRM (docker compose `osrm`, Israel extract prepared once into the `osrm-data`
  volume). gis-server `estimate_leg(mode="drive")` uses it (falls back to straight line x factor);
  `geocode` uses Nominatim (1 req/s, cached). The UI's "יוצאים מ" (browser location or address)
  is sent with the message; agent 4 adds drive time, route and departure time to the plan.
- Extension (user request, 2026-11-22): A places (`ingestion/places_loader.py` -> `pois` with day-plan
  categories; gis `find_places_near`), B day length + places around the hike (`day_plan.py`,
  `opening_hours.py`; agent 4 chooses, code times/drops). C remembering chat: `conversations` table (migration 004)
  loaded/saved by the API around each run (`terramind/conversations.py`); agent 1 returns
  request + change; `graph/scope.py` (code) decides "replan" (agents 2-3 skipped) vs "search".
- Step 5: README rewritten in Hebrew (replaces docs/ARCHITECTURE.md). LangSmith: `terramind/observability`
  exports LANGSMITH_* from .env at startup (pydantic-settings does not, so local runs never traced
  before). Verifying a real trace still needs a real LANGSMITH_API_KEY in .env.
- Alerts/closures still come from the Mock Alerts API (matched by trail name) until a real source is found.

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
docker compose up -d db osrm               # database (host port 5433) + OSRM (5000; first run prepares the map)
uv run python -m scripts.migrate           # schema
uv run python -m ingestion.osm_loader      # then derive_attributes, seed_knowledge, places_loader
uv run python -m ingestion.pipeline --once # weather + alerts into the DB (scripts.dev runs it on a schedule)
uv run python -m scripts.dev               # backend stack -> http://127.0.0.1:8000 (serves web/dist)
cd web; npm run dev                        # frontend dev server -> http://localhost:5174 (proxies API)
cd web; npm run build                      # production build into web/dist
uv run pytest                              # TERRAMIND_TEST_DATABASE_URL=...5433/terramind_test for DB tests
uv run ruff check . && uv run mypy src mcp_servers services ingestion scripts
```
