# TerraMind

Autonomous multi-agent hiking-route planner for northern Israel (Upper Galilee, Meron, Golan).
LangGraph orchestration with a safety veto loop, four FastMCP servers, Hybrid Agentic RAG over
PostGIS + pgvector, Claude via OpenRouter, Postgres memory, SSE streaming and a Leaflet map.

Full design: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Implementation notes and deviations: [below](#implementation-notes).

## Quick start (Windows / PowerShell)

Prerequisites: [uv](https://docs.astral.sh/uv/), Docker Desktop, Git. Python 3.12 is installed by uv.

```powershell
cd C:\projects\terramind
copy .env.example .env                   # then set ONE LLM provider (Anthropic/gateway or OpenRouter) in .env
uv sync --extra embeddings               # real bge-m3 embeddings (pulls torch, a few GB)
docker compose up -d db                  # PostgreSQL 17 + pgvector + PostGIS, on host port 5433

# Run these one at a time: each must succeed before the next.
uv run python -m scripts.migrate
uv run python -m ingestion.osm_loader    # Overpass download (tries 3 public mirrors), cached in ingestion/cache/
uv run python -m ingestion.derive_attributes
uv run python -m ingestion.seed_knowledge  # first run downloads BAAI/bge-m3 (~2 GB)

uv run python -m scripts.smoke_llm       # checks the OpenRouter key and model slugs
uv run python -m scripts.dev             # mock alerts + 4 MCP servers + API -> http://127.0.0.1:8000
```

**LLM provider.** With `ANTHROPIC_AUTH_TOKEN` (and optionally `ANTHROPIC_BASE_URL` for a gateway such
as OneProvider) the native Anthropic API is used, with prompt caching. Otherwise `OPENROUTER_API_KEY`
is used, with OpenRouter's server-side model fallbacks. `uv run python -m scripts.smoke_llm` checks
whichever is configured, including the model IDs.

No API key yet? Leave both empty: the system falls back to the offline
`HeuristicLLM` (keyword rules), so the whole pipeline, map and safety loop still run.
For a fast setup without torch, set `EMBEDDING_BACKEND=hash` (lexical only, demo quality).

Terminal-only demo (no servers to start, only the DB):

```powershell
uv run python -m scripts.demo_cli "אני רוצה לטייל בנחל כזיב מחר, מסלול מים" --scenario kziv_closed
```

## Demo scenarios (Mock Alerts API)

Switch from the UI dropdown, `POST /scenarios/{name}`, or `--scenario` in the CLI/evals.

| Scenario | What it tests |
| --- | --- |
| `normal` | Everything open |
| `kziv_closed` | Trail-level veto: Nahal Kziv closed, the graph loops back and picks another trail, then explains why |
| `golan_flood_warning` | FLOOD_RISK veto on Golan water/canyon trails |
| `crowded_holiday` | Soft issue (warning only) on popular water trails |
| `everything_closed` | Three vetoes, then `safe_fallback`: no plan rather than a guessed one |

## Tests, lint, evals

```powershell
docker compose exec db createdb -U terramind terramind_test
$env:TERRAMIND_TEST_DATABASE_URL = "postgresql://terramind:terramind@localhost:5433/terramind_test"
uv run pytest                      # 40 tests: unit + integration (the test DB gets a synthetic fixture)
uv run ruff check . ; uv run mypy src mcp_servers services ingestion evals scripts

uv run python -m evals.run_evals            # scenario suite: tool accuracy, safety loop, grounding
uv run python -m evals.run_evals --judge    # + LLM-as-a-judge faithfulness (needs OpenRouter)
uv run python -m evals.run_evals --langsmith  # record as a LangSmith experiment
```

Without `TERRAMIND_TEST_DATABASE_URL` the database tests are skipped and only unit tests run.

## Layout

```
src/terramind/        config, schemas (Pydantic), llm (router, OpenRouter client, heuristic),
                      graph (state, routing, nodes, builder), safety/rules.py, mcp_client (gateway),
                      api (FastAPI + SSE), runtime.py, regions.py, text.py, geo.py, embeddings.py
mcp_servers/          gis_server, knowledge_server, weather_server, alerts_server (FastMCP, HTTP)
services/mock_alerts_api/   FastAPI + scenarios/*.json
ingestion/            osm_loader, derive_attributes, seed_knowledge, data/tips.jsonl
evals/                datasets/scenarios.yaml, evaluators, run_evals.py, harness.py
frontend/             index.html, app.js (Leaflet + SSE reader), styles.css
db/migrations/        plain SQL, applied by scripts/migrate.py
tests/                unit/, integration/, fixtures/overpass_sample.json (synthetic)
```

## Implementation notes

What was built as designed, and where the code deliberately differs from the design doc:

- **No `langchain-mcp-adapters`.** Its current release requires `mcp<2`, while FastMCP 4 requires
  `mcp>=2`, so the two cannot be installed together. `mcp_client/client.py` is a thin gateway on
  `fastmcp.Client` instead: typed calls, per-call timeouts, failures returned as data, a per-run call
  log (used by the evals) and LangChain tools generated from the MCP schemas for native tool calling.
- **Supervisor = Haiku once per turn + pure-function routing.** The LLM classifies intent; every
  later decision is a unit-tested edge function in `graph/routing.py`.
- **Safety: rules first, LLM second.** `safety/rules.py` decides vetoes deterministically
  (closures, alerts, rain/flood, wind, heat, daylight, group fit). The Sonnet critic only reviews
  plans the rules accepted and can only add warnings. That keeps the veto loop reproducible for evals.
- **Timing vs trail vetoes.** A daylight/heat veto re-plans the same trail 90 minutes earlier
  (planner loop); a closure/flood veto excludes the trail and searches again (researcher loop).
  Max 3 attempts, then `safe_fallback`.
- **Planner: the model chooses, code computes.** Sonnet picks the trail and stops (with tool calls),
  but every time and distance is computed from the map data, so numbers in the answer are grounded.
- **Trade-offs are derived, not generated.** "Why B and not A" comes from the critic's verdicts
  (with the tool evidence), and the explainer phrases them.
- **Added tool `gis.find_trails_by_name`.** "Hike Nahal Kziv tomorrow" names a trail but no region;
  intake now infers the region from the trail instead of asking.
- **Memory.** Conversation in `AsyncPostgresSaver` (per thread), preferences in `AsyncPostgresStore`
  (per user, across threads). Domain models are registered with the checkpoint serializer.

## Known limitations

- Travel time is straight-line distance x a detour factor (no routing engine yet).
- Difficulty is a length heuristic until elevation (SRTM) is added.
- `ingestion/data/tips.jsonl` holds 25 short descriptions written for this project. Review them
  before relying on them, and add your own.
- Flash-flood risk is only partly visible in point forecasts (rain upstream). The Mock alerts and a
  conservative rule for water/canyon trails cover the MVP.
- `scripts/run_api.py` runs uvicorn on a SelectorEventLoop because psycopg (used by the Postgres
  checkpointer) does not support Windows' default ProactorEventLoop.
