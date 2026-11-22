# TerraMind — מסמך ארכיטקטורה ותוכנית MVP

Nov 19, 2026 · @Omri Halifa

## תקציר והחלטות מפתח

TerraMind היא גרף LangGraph אחד עם Supervisor, שש צמתי עבודה ושתי לולאות תיקון (שליפה חוזרת ווטו בטיחות). כל הגישה לדאטה עוברת דרך ארבעה שרתי MCP, ו-FastAPI מזרים את שלבי הריצה ל-Leaflet ב-SSE.

מיפוי חמשת הסוכנים מהמסמך המקורי לצמתים בגרף:

| סוכן במסמך | צומת בגרף | מודל | הערה |
| --- | --- | --- | --- |
| 1 — ניתוח הבקשה | `intake` | Haiku | חילוץ `TripRequest` ב-Structured Output |
| 2 — חיפוש במאגר | `researcher` + `grader` | ללא LLM / Haiku | Hybrid RAG: PostGIS + pgvector + FTS, ואחריו דירוג רלוונטיות |
| 3 — העשרת מידע | `enrich` + `safety_critic` | ללא LLM / Sonnet | שליפה מקבילית דטרמיניסטית, ואז שיפוט בטיחות עם וטו |
| 4 — תכנון ואופטימיזציה | `planner` | Sonnet | סדר תחנות, זמני הליכה ונסיעה, חלון אור יום |
| 5 — סיכום והצגה | `explainer` | Sonnet | Explainable Routing: טרייד-אופים סיבתיים |
| (חדש) מתזמר | `supervisor` | Haiku + כללים | ניתוב לפי כוונה ומצב ה-State |

החלטות שכדאי להכיר לפני כתיבת קוד:

- **כללים לפני LLM.** סגירת מסלול או התראה קריטית מטילות וטו דטרמיניסטי, בלי לשאול את המודל. ה-LLM שופט רק גורמים רכים (חום, עומס, התאמה לקבוצה). זה הופך את לולאת הבטיחות לניתנת לבדיקה ב-Evals.
- **Checkpointer לא מספיק לזיכרון בין סשנים.** `AsyncPostgresSaver` שומר היסטוריה לכל `thread_id`. העדפות משתמש שחוצות שיחות נשמרות ב-`AsyncPostgresStore` (namespace `("users", user_id)`). שניהם על אותו PostgreSQL.
- **וטו מנותב לפי סוג הבעיה.** בעיית תזמון (חוסר באור יום) חוזרת ל-`planner` עם שעת יציאה מוקדמת יותר. בעיה במסלול עצמו (סגור, סכנת שיטפון) חוזרת ל-`researcher`, והמסלול נכנס ל-`rejected_trail_ids`.
- **Embeddings מקומיים עם `BAAI/bge-m3`** (1024 ממדים, עברית ואנגלית). כך אין תלות בזמינות endpoint של Embeddings ב-OpenRouter, ואין עלות לכל שאילתה.
- **מזהי המודלים רק ב-`.env`.** ב-OpenRouter קיימים היום Claude Sonnet 5.5 ו-Claude Haiku 5.5. הקוד לא מקודד slug קשיח, כדי שהחלפת גרסה לא תדרוש שינוי קוד.
- **מימוש החידוש.** הסברתיות (כיוון 3 במסמך) נכנסת ל-MVP. תכנון "חי" עם GPS (כיוון 1) הוא שלב Stretch, והארכיטקטורה מוכנה לו: אותו גרף, טריגר אחר.

## 1. תרשים ארכיטקטורה

שני תרשימים: הראשון מראה את המערכת כולה וזרימת הנתונים, והשני מראה את הגרף של LangGraph עם הקשתות המותנות. התוויות באנגלית, כדי שהרינדור יעבוד בכל עורך Mermaid.

### 1א. מבט מערכת

```mermaid
flowchart LR
  subgraph Client
    UI["Leaflet UI (static JS)"]
  end
  subgraph API["FastAPI (async)"]
    R["POST /chat/stream (SSE)"]
    T["GET /trails/{id}/geojson, /threads"]
  end
  subgraph Runtime["LangGraph runtime"]
    G["TerraMind graph"]
    CP[("AsyncPostgresSaver - checkpoints")]
    ST[("AsyncPostgresStore - user profiles")]
    MC["MultiServerMCPClient"]
  end
  subgraph MCP["MCP servers (FastMCP, streamable HTTP)"]
    GIS["gis-server :8101"]
    KN["knowledge-server :8102"]
    WX["weather-server :8103"]
    AL["alerts-server :8104"]
  end
  subgraph Data
    PG[("PostgreSQL 17 + PostGIS + pgvector")]
    EMB["bge-m3 (sentence-transformers)"]
  end
  ING["Ingestion: OSMnx / Overpass + seed tips"]
  OM["Open-Meteo API"]
  MOCK["Mock Alerts API (FastAPI) :8200"]
  OR["OpenRouter: Claude Sonnet / Haiku"]
  LS["LangSmith"]

  UI -- "user message" --> R
  R -- "SSE: node / token / tool / plan events" --> UI
  UI -- "GeoJSON" --> T
  R --> G
  G <--> CP
  G <--> ST
  G --> MC
  G -- "chat completions + tool calls" --> OR
  MC --> GIS & KN & WX & AL
  GIS --> PG
  KN --> PG
  KN --> EMB
  WX --> OM
  AL --> MOCK
  ING --> PG
  CP --- PG
  ST --- PG
  G -. "traces, tokens, cost" .-> LS
```

### 1ב. גרף LangGraph והלולאות

```mermaid
flowchart TD
  S((START)) --> LP[load_profile]
  LP --> SUP{supervisor - Haiku + rules}
  SUP -- "new / refine plan" --> IN[intake - Haiku, structured output]
  IN --> SUP
  SUP -- "missing required fields" --> CL[ask_clarification]
  CL --> E((END))
  SUP -- "question about current plan" --> EX
  SUP -- "request ready" --> RS[researcher - spatial + semantic via MCP]
  RS --> GR[grader - Haiku relevance]
  GR -- "too few relevant, round < 2: rewrite query" --> RS
  GR -- "no candidates at all" --> FB
  GR -- "enough evidence" --> PL[planner - Sonnet]
  PL --> EN[enrich - weather, daylight, alerts in parallel]
  EN --> SC{safety_critic - hard rules + Sonnet}
  SC -- "approved" --> EX[explainer - Sonnet]
  SC -- "timing issue only, attempt < 3" --> PL
  SC -- "trail vetoed, attempt < 3" --> RS
  SC -- "attempts exhausted" --> FB[safe_fallback]
  FB --> EX
  EX --> SM[save_memory]
  SM --> E
```

שתי לולאות חסומות במונה: לולאת השליפה (`research_rounds` ≤ 2) ולולאת הבטיחות (`attempts` ≤ 3). בנוסף, `recursion_limit=40` ב-config של הריצה משמש רשת ביטחון שנייה. `safe_fallback` לא ממציא מסלול: הוא מחזיר את המועמד הבטוח ביותר עם אזהרות מפורשות, או ממליץ לדחות את הטיול.

## 2. מבנה תיקיות

מונו-רפו עם `uv`. האפליקציה (גרף + API) ב-`src/terramind`. כל שרת MCP הוא תהליך נפרד תחת `mcp_servers/`, והוא לא מייבא דבר מ-`terramind.graph`. כך הגבול בין הסוכנים לכלים נשמר אמיתי.

```text
terramind/
├── pyproject.toml                # uv workspace, ruff, mypy --strict, pytest-asyncio
├── .env.example
├── docker-compose.yml
├── docker/
│   ├── postgres/
│   │   ├── Dockerfile            # postgis/postgis + pgvector
│   │   └── init/01_extensions.sql
│   └── python.Dockerfile         # one image, different CMD per service
├── db/
│   └── migrations/               # plain SQL: 001_gis.sql, 002_knowledge.sql
├── src/terramind/
│   ├── config.py                 # pydantic-settings: Settings, model IDs, timeouts
│   ├── llm/
│   │   ├── router.py             # task -> tier -> ChatOpenAI (OpenRouter)
│   │   ├── caching.py            # cache_control helpers for static system prompts
│   │   └── usage.py              # token / cost extraction from OpenRouter responses
│   ├── schemas/                  # Pydantic domain models (no LangGraph imports)
│   │   ├── trip.py  retrieval.py  safety.py  plan.py  events.py
│   ├── graph/
│   │   ├── state.py              # TerraMindState + reducers
│   │   ├── builder.py            # StateGraph wiring, compile(checkpointer, store)
│   │   ├── routing.py            # pure edge functions (unit-tested)
│   │   ├── nodes/
│   │   │   ├── supervisor.py  intake.py  researcher.py  grader.py
│   │   │   ├── planner.py  enrich.py  safety_critic.py  explainer.py
│   │   │   └── memory.py         # load_profile / save_memory
│   │   └── prompts/              # *.md system prompts (cacheable, versioned)
│   ├── safety/rules.py           # deterministic veto rules
│   ├── mcp_client/client.py      # MultiServerMCPClient config + tool filtering per node
│   ├── api/
│   │   ├── main.py               # FastAPI app, lifespan (pool, checkpointer, MCP client)
│   │   ├── routes/chat.py        # POST /chat/stream -> SSE
│   │   ├── routes/trails.py      # GeoJSON for the map
│   │   └── sse.py                # graph stream -> typed SSE events
│   └── observability/tracing.py  # LangSmith metadata, tags, run names
├── mcp_servers/
│   ├── common/                   # asyncpg pool, httpx client w/ retry+timeout, errors
│   ├── gis_server/server.py      # + queries.py
│   ├── knowledge_server/server.py# + embeddings.py, hybrid.py (RRF)
│   ├── weather_server/server.py  # + open_meteo.py, TTL cache
│   └── alerts_server/server.py
├── services/mock_alerts_api/     # FastAPI + scenarios/*.json (closures, floods, crowding)
├── ingestion/
│   ├── osm_loader.py             # Overpass -> trails, pois (PostGIS)
│   ├── derive_attributes.py      # length, loop?, water/view tags, difficulty heuristic
│   ├── seed_knowledge.py         # chunk + embed tips into pgvector
│   └── data/tips.jsonl           # curated HE/EN descriptions & tips
├── evals/
│   ├── datasets/scenarios.yaml   # inputs + expected tools + mock scenario + expected verdict
│   ├── run_evals.py              # langsmith.aevaluate entrypoint
│   └── evaluators/               # faithfulness.py, tool_accuracy.py, safety_loop.py
├── frontend/                     # index.html, app.js (Leaflet + EventSource), styles.css
└── tests/
    ├── unit/                     # routing, rules, reducers, schemas
    └── integration/              # MCP servers against test DB, graph with fake LLM
```

### Docker Compose

התמונה הרשמית `postgis/postgis` לא כוללת pgvector, ולכן בונים שכבה דקה מעליה. בפיתוח מספיק `docker compose up db mock-alerts`, ושאר השירותים רצים מקומית עם `uv run`. הפרופיל `full` מרים את הכול לדמו.

```dockerfile
# docker/postgres/Dockerfile
FROM postgis/postgis:17-3.5
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-17-pgvector \
 && rm -rf /var/lib/apt/lists/*
```

```sql
-- docker/postgres/init/01_extensions.sql
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- fuzzy trail-name matching (HE/EN)
```

```yaml
# docker-compose.yml
x-py: &py
  build: { context: ., dockerfile: docker/python.Dockerfile }
  env_file: .env
  depends_on:
    db: { condition: service_healthy }
  profiles: ["full"]

services:
  db:
    build: ./docker/postgres
    environment:
      POSTGRES_USER: terramind
      POSTGRES_PASSWORD: terramind
      POSTGRES_DB: terramind
    ports: ["5432:5432"]
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./docker/postgres/init:/docker-entrypoint-initdb.d:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U terramind -d terramind"]
      interval: 5s
      retries: 10

  mock-alerts:
    <<: *py
    depends_on: []
    profiles: []                 # always on
    command: uvicorn services.mock_alerts_api.main:app --host 0.0.0.0 --port 8200
    ports: ["8200:8200"]

  mcp-gis:
    <<: *py
    command: python -m mcp_servers.gis_server.server
    ports: ["8101:8101"]
  mcp-knowledge:
    <<: *py
    command: python -m mcp_servers.knowledge_server.server
    ports: ["8102:8102"]
    volumes: ["hf-cache:/root/.cache/huggingface"]   # keep bge-m3 weights
  mcp-weather:
    <<: *py
    depends_on: []
    command: python -m mcp_servers.weather_server.server
    ports: ["8103:8103"]
  mcp-alerts:
    <<: *py
    depends_on: [mock-alerts]
    command: python -m mcp_servers.alerts_server.server
    ports: ["8104:8104"]

  api:
    <<: *py
    command: uvicorn terramind.api.main:app --host 0.0.0.0 --port 8000
    ports: ["8000:8000"]
    depends_on:
      db: { condition: service_healthy }
      mcp-gis: { condition: service_started }
      mcp-knowledge: { condition: service_started }
      mcp-weather: { condition: service_started }
      mcp-alerts: { condition: service_started }

volumes:
  pgdata:
  hf-cache:
```

## 3. שכבת המודלים (OpenRouter)

כל צומת מבקש מודל לפי שם המשימה שלו, ולא לפי שם המודל. ההחלטה בין Sonnet ל-Haiku יושבת במקום אחד.

| משימה | Tier | משתנה סביבה | פלט |
| --- | --- | --- | --- |
| `supervisor` | FAST | `MODEL_FAST` | `RouteDecision` |
| `intake` | FAST | `MODEL_FAST` | `TripRequest` |
| `grader` | FAST | `MODEL_FAST` | `list[RelevanceGrade]` |
| `planner` | SMART | `MODEL_SMART` | `RoutePlan` |
| `safety_critic` | SMART | `MODEL_SMART` | `SafetyVerdict` |
| `explainer` | SMART | `MODEL_SMART` | `FinalAnswer` (טקסט בסטרימינג) |

```python
# src/terramind/llm/router.py
from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from terramind.config import Settings, get_settings


class ModelTier(StrEnum):
    FAST = "fast"    # Haiku: routing, extraction, grading
    SMART = "smart"  # Sonnet: planning, safety, explanation


Task = Literal["supervisor", "intake", "grader", "planner", "safety_critic", "explainer"]

TASK_TIERS: dict[Task, ModelTier] = {
    "supervisor": ModelTier.FAST,
    "intake": ModelTier.FAST,
    "grader": ModelTier.FAST,
    "planner": ModelTier.SMART,
    "safety_critic": ModelTier.SMART,
    "explainer": ModelTier.SMART,
}


def _build(model_id: str, fallbacks: list[str], s: Settings, temperature: float) -> ChatOpenAI:
    return ChatOpenAI(
        model=model_id,
        base_url=s.openrouter_base_url,          # https://openrouter.ai/api/v1
        api_key=s.openrouter_api_key,
        temperature=temperature,
        timeout=s.llm_timeout_s,                  # hard per-request timeout
        max_retries=s.llm_max_retries,            # transport-level retries (429/5xx)
        default_headers={"HTTP-Referer": s.app_url, "X-Title": "TerraMind"},
        # Server-side fallback: OpenRouter tries these models in order if the primary fails.
        extra_body={"models": [model_id, *fallbacks]},
    )


@lru_cache(maxsize=16)
def get_chat_model(task: Task, temperature: float = 0.0) -> BaseChatModel:
    s = get_settings()
    tier = TASK_TIERS[task]
    if tier is ModelTier.FAST:
        return _build(s.model_fast, s.model_fast_fallbacks, s, temperature)
    return _build(s.model_smart, s.model_smart_fallbacks, s, temperature)


def get_structured_model[T: BaseModel](task: Task, schema: type[T]) -> Runnable[object, T]:
    """Structured output via native tool calling (most portable path through OpenRouter)."""
    return get_chat_model(task).with_structured_output(schema, method="function_calling")
```

```bash
# .env.example
OPENROUTER_API_KEY=sk-or-...
MODEL_SMART=anthropic/claude-sonnet-5.5      # verify slug on openrouter.ai/anthropic
MODEL_FAST=anthropic/claude-haiku-5.5
MODEL_SMART_FALLBACKS='["anthropic/claude-sonnet-5"]'
MODEL_FAST_FALLBACKS='["anthropic/claude-haiku-4.5"]'
LLM_TIMEOUT_S=45
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=...
LANGSMITH_PROJECT=terramind-dev
DATABASE_URL=postgresql://terramind:terramind@localhost:5432/terramind
```

עקרונות נוספים בשכבה הזו:

- **Prompt Caching.** ה-System Prompt הסטטי של כל צומת נשלח כ-content block עם `cache_control: {"type": "ephemeral"}`, ו-OpenRouter מעביר אותו ל-Anthropic. הסדר תמיד קבוע: הוראות, אחריהן תיאור הכלים ודוגמאות, ורק בסוף הקונטקסט המשתנה. לקאשינג יש סף מינימלי של טוקנים, ולכן הוא משתלם בעיקר ב-planner וב-critic. את שיעור הפגיעה מודדים משדות ה-usage של התשובה.
- **שתי שכבות Fallback.** שכבה אחת היא `models` בצד של OpenRouter (עומס אצל הספק). השנייה היא `.with_fallbacks()` של LangChain בצד הלקוח, ל-timeout או לשגיאת רשת. כל שימוש ב-fallback מסומן כ-tag ב-LangSmith.
- **Embeddings.** מודל `BAAI/bge-m3` נטען פעם אחת ב-`knowledge-server`, מפיק וקטורים מנורמלים של 1024 ממדים, ורץ ב-`asyncio.to_thread` כדי לא לחסום את ה-event loop. אותו מודל משמש גם ב-ingestion וגם בשאילתות.

## 4. הגדרת ה-State

ה-State של הגרף הוא `TypedDict`, ובתוכו מודלים של Pydantic שהם immutable (`frozen=True`). השדות שמצטברים בתוך לולאה (`verdict_history`, `rejected_trail_ids`, `errors`) מקבלים reducer שאפשר לאפס בתחילת תוכנית חדשה. בלי איפוס כזה, ה-checkpointer היה גורר מסלולים פסולים משיחה קודמת. בדקתי את הקוד מול LangGraph 1.2: גרף דמה עם שלושה וטו רצופים הגיע ל-`safe_fallback`, והאיפוס עבד בין ריצות. נדרש Python 3.12+ (בגלל תחביר ה-generics של PEP 695).

### מודלי הדומיין (`src/terramind/schemas/`)

```python
# src/terramind/schemas/*.py  (shown together for brevity; re-exported from __init__)
from __future__ import annotations

from datetime import date, datetime, time
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

LatLon = tuple[float, float]  # (lat, lon), WGS84


class Frozen(BaseModel):
    """Base for state payloads: immutable, strict about unknown fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------- trip.py ----------
class Difficulty(StrEnum):
    EASY = "easy"
    MODERATE = "moderate"
    HARD = "hard"


class TrailType(StrEnum):
    WATER = "water"
    VIEWPOINT = "viewpoint"
    FOREST = "forest"
    HISTORY = "history"
    LOOP = "loop"
    FAMILY = "family"


class GroupProfile(Frozen):
    adults: int = Field(default=2, ge=1, le=50)
    children: int = Field(default=0, ge=0, le=50)
    min_child_age: int | None = Field(default=None, ge=0, le=17)
    fitness: Literal["low", "medium", "high"] = "medium"
    pace_kmh: float = Field(default=3.5, gt=0.5, lt=8.0)  # digital-twin hook


class TripRequest(Frozen):
    """Structured parameters extracted from free text by the intake node."""

    region: str | None = Field(default=None, description="e.g. 'Upper Galilee', 'Carmel'")
    trip_date: date | None = None
    start_time: time | None = None
    max_duration_hours: float | None = Field(default=None, gt=0, le=14)
    difficulty: Difficulty | None = None
    trail_types: list[TrailType] = Field(default_factory=list)
    max_length_km: float | None = Field(default=None, gt=0, le=40)
    max_drive_minutes: int | None = Field(default=None, gt=0, le=300)
    origin: LatLon | None = Field(default=None, description="Where the group drives from")
    group: GroupProfile = Field(default_factory=GroupProfile)
    must_have: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    semantic_query: str = Field(description="One-line normalized intent used for vector search")
    missing_fields: list[Literal["region", "trip_date"]] = Field(default_factory=list)


class UserProfile(Frozen):
    """Long-term preferences, persisted in AsyncPostgresStore (cross-thread)."""

    user_id: str
    preferred_difficulty: Difficulty | None = None
    favorite_types: list[TrailType] = Field(default_factory=list)
    default_group: GroupProfile = Field(default_factory=GroupProfile)
    completed_trail_ids: list[str] = Field(default_factory=list)
    disliked: list[str] = Field(default_factory=list)


# ---------- retrieval.py ----------
class TrailCandidate(Frozen):
    trail_id: str
    name: str
    name_he: str | None = None
    region: str
    length_km: float
    elevation_gain_m: float | None = None
    difficulty: Difficulty | None = None
    tags: list[str] = Field(default_factory=list)
    is_loop: bool | None = None
    trailhead: LatLon
    spatial_rank: int | None = None
    semantic_rank: int | None = None
    fused_score: float = 0.0  # Reciprocal Rank Fusion


class EvidenceChunk(Frozen):
    chunk_id: str
    trail_id: str | None
    text: str
    source: str
    score: float


class RelevanceGrade(Frozen):
    chunk_id: str
    relevant: bool
    reason: str = Field(max_length=200)


# ---------- safety.py ----------
class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class WeatherSnapshot(Frozen):
    trail_id: str
    for_date: date
    temp_max_c: float | None
    apparent_temp_max_c: float | None
    precip_prob_max_pct: int | None
    precip_sum_mm: float | None
    wind_gust_max_kmh: float | None
    uv_index_max: float | None
    sunrise: datetime | None
    sunset: datetime | None
    hourly_precip_prob: dict[int, int] = Field(default_factory=dict)  # hour -> %
    status: Literal["ok", "cached", "out_of_range", "unavailable"] = "ok"


class TrailAlert(Frozen):
    alert_id: str
    trail_id: str | None
    region: str | None
    kind: Literal["closure", "flood", "fire", "crowding", "military", "maintenance"]
    severity: Severity
    message: str
    valid_from: datetime
    valid_to: datetime | None


IssueCode = Literal[
    "CLOSED", "HEAT", "FLOOD_RISK", "STORM", "INSUFFICIENT_DAYLIGHT",
    "DIFFICULTY_MISMATCH", "CROWDED", "DATA_UNAVAILABLE",
]
TIMING_CODES: frozenset[str] = frozenset({"INSUFFICIENT_DAYLIGHT", "HEAT"})


class SafetyIssue(Frozen):
    trail_id: str
    code: IssueCode
    severity: Severity
    evidence: str = Field(description="Cites the tool result that triggered the issue")
    source: Literal["rule", "llm"]


class SafetyVerdict(Frozen):
    approved: bool
    issues: list[SafetyIssue] = Field(default_factory=list)
    rejected_trail_ids: list[str] = Field(default_factory=list)
    replan_scope: Literal["none", "timing", "trail"] = "none"
    guidance_for_replan: str | None = None


# ---------- plan.py ----------
class Stop(Frozen):
    order: int = Field(ge=0)
    name: str
    kind: Literal["parking", "trailhead", "poi", "viewpoint", "water", "food", "end"]
    location: LatLon
    arrive_at: time | None = None
    dwell_minutes: int = Field(default=0, ge=0)
    note: str | None = None


class RoutePlan(Frozen):
    plan_id: str
    trail_id: str
    title: str
    stops: list[Stop] = Field(min_length=2)
    walk_km: float
    walk_minutes: int
    drive_minutes: int | None
    start_time: time
    end_time: time
    alternatives_considered: list[str] = Field(default_factory=list)


class TradeOff(Frozen):
    chosen_trail_id: str
    rejected_trail_id: str
    factor: IssueCode | Literal["DISTANCE", "PREFERENCE_MATCH", "SCENERY"]
    detail: str  # e.g. "rain probability 70% after 14:00 on an exposed ridge vs 10%"


class FinalAnswer(Frozen):
    summary_md: str
    plan: RoutePlan | None  # None => safe_fallback recommended postponing
    tradeoffs: list[TradeOff] = Field(default_factory=list)
    safety_notes: list[str] = Field(default_factory=list)
    tips: list[str] = Field(default_factory=list)
    cited_chunk_ids: list[str] = Field(default_factory=list)  # used by faithfulness eval


class NodeError(Frozen):
    node: str
    kind: Literal["timeout", "tool_error", "validation", "llm_error"]
    message: str
    recoverable: bool
```

### State ו-Reducers (`graph/state.py`)

```python
# src/terramind/graph/state.py
from __future__ import annotations

from typing import Annotated, Literal, NotRequired, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from terramind.schemas import (
    EvidenceChunk, FinalAnswer, NodeError, RoutePlan, SafetyVerdict,
    TrailAlert, TrailCandidate, TripRequest, UserProfile, WeatherSnapshot,
)

RESET: list[str] = []  # sentinel object: identity-checked by the reducers below


def union_or_reset(left: list[str] | None, right: list[str] | None) -> list[str]:
    """Ordered set-union; returning RESET from a node clears the channel for a new plan."""
    if right is RESET:
        return []
    return list(dict.fromkeys([*(left or []), *(right or [])]))


def append_or_reset[T](left: list[T] | None, right: list[T] | None) -> list[T]:
    if right is RESET:
        return []
    return [*(left or []), *(right or [])]


Intent = Literal["new_plan", "refine_plan", "question", "smalltalk"]
NextNode = Literal[
    "intake", "ask_clarification", "researcher", "planner", "explainer", "end",
]


class TerraMindState(TypedDict):
    # --- conversation (persisted per thread by AsyncPostgresSaver) ---
    messages: Annotated[list[AnyMessage], add_messages]
    user_id: str

    # --- routing ---
    intent: NotRequired[Intent]
    next_node: NotRequired[NextNode]

    # --- request & memory ---
    request: NotRequired[TripRequest]
    profile: NotRequired[UserProfile]  # loaded from the Store, read-only inside the run

    # --- hybrid RAG (overwritten each research round) ---
    candidates: NotRequired[list[TrailCandidate]]
    evidence: NotRequired[list[EvidenceChunk]]  # only chunks that passed the grader
    research_rounds: NotRequired[int]
    rewritten_query: NotRequired[str]

    # --- planning & safety loop ---
    draft_plan: NotRequired[RoutePlan]
    weather: NotRequired[dict[str, WeatherSnapshot]]  # trail_id -> snapshot
    alerts: NotRequired[list[TrailAlert]]
    verdict: NotRequired[SafetyVerdict]
    verdict_history: Annotated[list[SafetyVerdict], append_or_reset]  # explainer input
    rejected_trail_ids: Annotated[list[str], union_or_reset]
    attempts: NotRequired[int]

    # --- output & diagnostics ---
    final: NotRequired[FinalAnswer]
    errors: Annotated[list[NodeError], append_or_reset]


MAX_RESEARCH_ROUNDS = 2
MAX_SAFETY_ATTEMPTS = 3
MIN_RELEVANT_CHUNKS = 3
```

### פונקציות הקשתות המותנות (`graph/routing.py`)

```python
# src/terramind/graph/routing.py  -- pure functions, unit-tested without any LLM
from __future__ import annotations

from typing import Literal

from terramind.graph.state import (
    MAX_RESEARCH_ROUNDS, MAX_SAFETY_ATTEMPTS, MIN_RELEVANT_CHUNKS, TerraMindState,
)


def route_after_grader(state: TerraMindState) -> Literal["researcher", "planner", "safe_fallback"]:
    candidates = state.get("candidates", [])
    if not candidates:
        return "safe_fallback"
    if (
        len(state.get("evidence", [])) < MIN_RELEVANT_CHUNKS
        and state.get("research_rounds", 0) < MAX_RESEARCH_ROUNDS
    ):
        return "researcher"  # grader already wrote `rewritten_query`
    return "planner"


def route_after_critic(
    state: TerraMindState,
) -> Literal["explainer", "planner", "researcher", "safe_fallback"]:
    verdict = state["verdict"]
    if verdict.approved:
        return "explainer"
    if state.get("attempts", 0) >= MAX_SAFETY_ATTEMPTS:
        return "safe_fallback"
    return "planner" if verdict.replan_scope == "timing" else "researcher"
```

### רישום הטיפוסים ב-Checkpointer

הבדיקה העלתה ממצא: `JsonPlusSerializer` מתריע על טיפוסי Pydantic שלא נרשמו, ולפי האזהרה גרסה עתידית תחסום אותם. לכן רושמים את כל מודלי `terramind.schemas` מראש:

```python
# src/terramind/graph/builder.py (excerpt)
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.store.postgres.aio import AsyncPostgresStore

from terramind import schemas


def _allowed_types() -> list[tuple[str, str]]:
    """Register every domain model so checkpoints can deserialize them safely."""
    return [
        (obj.__module__, name)
        for name, obj in vars(schemas).items()
        if isinstance(obj, type) and obj.__module__.startswith("terramind.schemas")
    ]


SERDE = JsonPlusSerializer(allowed_msgpack_modules=_allowed_types())

# inside the FastAPI lifespan:
# async with AsyncPostgresSaver.from_conn_string(dsn, serde=SERDE) as saver, \
#            AsyncPostgresStore.from_conn_string(dsn) as store:
#     await saver.setup(); await store.setup()
#     app.state.graph = build_graph().compile(checkpointer=saver, store=store)
```

## 5. כלי שרתי ה-MCP

יש ארבעה שרתי FastMCP, וכל אחד רץ ב-transport של streamable HTTP על פורט משלו. כל צומת מקבל רק את הכלים שהוא צריך (עקרון least privilege), וזה גם מה שמאפשר למדוד Tool Calling Accuracy. כל כלי מחזיר מודל Pydantic שמסורבל ל-JSON, ובמקרה של כשל הוא מחזיר `status` מפורש ולא מעלה חריגה. כך הסוכן יכול לנמק את הכשל.

### gis-server (PostGIS) — פורט 8101

| Tool | קלט עיקרי | מחזיר | משתמש |
| --- | --- | --- | --- |
| `list_regions` | — | שמות אזורים + bbox | intake |
| `search_trails` | `region`, `difficulty`, `min_km`/`max_km`, `tags`, `loop_only`, `exclude_ids`, `near` + `max_distance_km`, `limit` | `list[TrailCandidate]` מדורגת | researcher |
| `get_trail_details` | `trail_id` | מטא-דטה, אורך, טיפוס, סימון שביל (`osmc:symbol`) | planner |
| `find_pois_near_trail` | `trail_id`, `buffer_m`, `categories` | נקודות עניין לאורך השביל (`ST_DWithin` על geography) | planner |
| `estimate_leg` | `from`, `to`, `mode` (walk/drive) | מרחק וזמן משוער (ב-MVP: מרחק גיאודזי × מקדם פיתול) | planner |
| `get_trail_geojson` | `trail_id` | GeoJSON למפה (משמש גם את ה-API) | api |

### knowledge-server (pgvector + FTS) — פורט 8102

| Tool | קלט עיקרי | מחזיר | משתמש |
| --- | --- | --- | --- |
| `hybrid_search` | `query`, `trail_ids?`, `region?`, `k` | `list[EvidenceChunk]`: וקטור + full-text, מאוחדים ב-RRF | researcher |
| `semantic_search` | `query`, `k`, `filters` | וקטורי בלבד (בסיס להשוואה ב-Evals) | researcher |
| `get_trail_notes` | `trail_id`, `k` | טיפים וביקורות למסלול ספציפי | planner, explainer |

### weather-server (Open-Meteo) — פורט 8103

| Tool | קלט עיקרי | מחזיר | משתמש |
| --- | --- | --- | --- |
| `get_forecast` | `lat`, `lon`, `date`, `trail_id` | `WeatherSnapshot`: daily + hourly `precipitation_probability`, `timezone=Asia/Jerusalem` | enrich |
| `get_daylight` | `lat`, `lon`, `date` | זריחה, שקיעה ומשך אור היום | enrich |
| `get_forecast_batch` | `points[]`, `date` | snapshot לכל מועמד (קריאה אחת, בלי N+1) | enrich |

### alerts-server (Mock API) — פורט 8104

| Tool | קלט עיקרי | מחזיר | משתמש |
| --- | --- | --- | --- |
| `get_trail_status` | `trail_id`, `date` | open/closed + סיבה | enrich |
| `list_active_alerts` | `region` או `bbox`, `date` | `list[TrailAlert]` | enrich |
| `get_crowd_level` | `trail_id`, `date` | low/medium/high | enrich |

סימולציית תרחישים נעשית רק דרך נתיב ה-admin של ה-Mock API (`POST /scenarios/{name}`), ולא ככלי MCP. כך הסוכן לא יכול "לפתוח" נחל סגור.

### דוגמת שרת: טיפול ב-Timeouts וב-Cache

```python
# mcp_servers/weather_server/server.py
from __future__ import annotations

from datetime import date, timedelta

import httpx
from cachetools import TTLCache
from fastmcp import FastMCP

from mcp_servers.weather_server.open_meteo import parse_snapshot
from terramind.schemas import WeatherSnapshot

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
FORECAST_HORIZON_DAYS = 16

mcp = FastMCP("terramind-weather")
_cache: TTLCache[tuple[float, float, date], WeatherSnapshot] = TTLCache(maxsize=512, ttl=1800)
_client = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=3.0, read=8.0, write=3.0, pool=3.0),
    transport=httpx.AsyncHTTPTransport(retries=2),  # connection-level retries
)


@mcp.tool
async def get_forecast(trail_id: str, lat: float, lon: float, for_date: date) -> WeatherSnapshot:
    """Daily + hourly forecast for a trailhead. Never raises: returns status instead."""
    if for_date > date.today() + timedelta(days=FORECAST_HORIZON_DAYS):
        return WeatherSnapshot.unavailable(trail_id, for_date, status="out_of_range")

    key = (round(lat, 2), round(lon, 2), for_date)  # ~1 km grid
    if (hit := _cache.get(key)) is not None:
        return hit.model_copy(update={"trail_id": trail_id, "status": "cached"})

    params = {
        "latitude": lat, "longitude": lon, "timezone": "Asia/Jerusalem",
        "start_date": for_date.isoformat(), "end_date": for_date.isoformat(),
        "daily": "temperature_2m_max,apparent_temperature_max,precipitation_sum,"
                 "precipitation_probability_max,wind_gusts_10m_max,uv_index_max,sunrise,sunset",
        "hourly": "precipitation_probability",
    }
    try:
        resp = await _client.get(OPEN_METEO_URL, params=params)
        resp.raise_for_status()
    except (httpx.TimeoutException, httpx.HTTPError):
        # The critic treats this as DATA_UNAVAILABLE (warning), not as "safe".
        return WeatherSnapshot.unavailable(trail_id, for_date, status="unavailable")

    snapshot = parse_snapshot(trail_id, for_date, resp.json())
    _cache[key] = snapshot
    return snapshot


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8103)
```

`WeatherSnapshot.unavailable(...)` הוא classmethod קטן שממלא `None` בכל השדות. בצד הסוכנים, `MultiServerMCPClient` מ-`langchain-mcp-adapters` מתחבר לארבעת השרתים (`{"url": "http://localhost:8103/mcp", "transport": "streamable_http"}`). הוא נוצר פעם אחת ב-lifespan, והכלים מסוננים לפי צומת. ה-Hybrid RAG עצמו עובד כך: סינון מרחבי קשיח ב-PostGIS קובע את קבוצת המועמדים, חיפוש וקטורי ו-FTS מדרגים בתוכה, RRF מאחד את הדירוגים, ו-Haiku מסנן את מה שלא רלוונטי.

## 6. תוכנית עבודה מדורגת ל-MVP

התוכנית מחולקת ל-9 שלבים, וכל אחד מסתיים במשהו שרץ ונבדק. LangSmith מחובר כבר משלב 0 (רק משתני סביבה), וסט תרחישי ה-Evals נבנה תוך כדי העבודה ולא בסוף. שלבים 0–5 הם קו הדמו מקצה לקצה ב-CLI. השלבים שאחריהם מוסיפים את שכבת ה-Production.

| שלב | מה בונים | סיום = |
| --- | --- | --- |
| 0 — שלד | `pyproject` (uv, ruff, mypy strict, pytest-asyncio), `config.py`, Docker ל-DB, `.env`, LangSmith | `docker compose up db` והרחבות קיימות; קריאת Haiku אחת מופיעה ב-LangSmith |
| 1 — דאטה | סכמת `trails`/`pois`/`knowledge_chunks`, טעינת OSM לאזור אחד (יחסי `route=hiking`), גזירת תכונות, בערך 60 טיפים HE/EN עם embeddings | שאילתת SQL מחזירה מסלולי מים ברדיוס נתון; חיפוש וקטורי בעברית מחזיר תוצאות סבירות |
| 2 — שרתי MCP | 4 שרתים + Mock Alerts API עם תרחישים (סגירה, שיטפון, עומס) | כל כלי נבדק ב-MCP Inspector וב-integration test; ניתוק רשת מ-Open-Meteo מחזיר `unavailable` ולא חריגה |
| 3 — מודלים ו-Intake | `llm/router.py`, `intake` + `supervisor` + `ask_clarification`, פרומפטים עם cache | 20 בקשות חופשיות בעברית נותנות `TripRequest` תקין; בקשה בלי תאריך מובילה לשאלת הבהרה |
| 4 — Hybrid RAG | `researcher` (מרחבי + סמנטי במקביל, `asyncio.gather`), `grader` עם שכתוב שאילתה, לולאת שליפה | בסט הבדיקה, השליפה ההיברידית מנצחת חיפוש וקטורי בלבד ב-hit@5 |
| 5 — תכנון ובטיחות | `planner`, `enrich`, `safety/rules.py`, `safety_critic`, `safe_fallback`, `explainer` | דמו CLI מקצה לקצה. תרחיש "נחל סגור" מסתיים במסלול חלופי עם הסבר טרייד-אוף |
| 6 — זיכרון | `AsyncPostgresSaver` + `AsyncPostgresStore`, `load_profile`/`save_memory`, רישום serde | שיחה שנייה (thread חדש) זוכרת "משפחה עם ילדים בני 6"; המשך באותו thread משנה תוכנית קיימת |
| 7 — API ו-Streaming | FastAPI lifespan, `POST /chat/stream` עם `astream(stream_mode=["updates", "messages", "custom"])` שמומר לאירועי SSE מטופסים, `get_stream_writer` לשלבי החשיבה, disconnect ו-heartbeat | `curl -N` מציג את הצמתים ואת טוקני ה-explainer בזמן אמת |
| 8 — ממשק Leaflet | צ'אט + פאנל שלבים, שכבת GeoJSON למסלול הנבחר, מסלולים שנפסלו באפור מקווקו, תחנות ממוספרות עם popup הסבר | דמו הגנה: בקשה → וטו נראה על המפה → מסלול חלופי |
| 9 — Evals ו-Observability | בערך 30 תרחישים ב-`scenarios.yaml`, `langsmith.aevaluate`, שלושה evaluators, דוח עלויות, latency ו-cache hit | דוח השוואה בין שתי גרסאות פרומפט ב-LangSmith |
| Stretch | תכנון חי עם GPS: `POST /trips/{id}/position` מפעיל שוב את `enrich` → `safety_critic` על אותו thread. אחר כך: משא ומתן בין סוכנים, Digital Twin מ-`pace_kmh` | — |

### מה ה-Evals מודדים

- **Faithfulness.** Sonnet משמש כשופט ובודק שכל טענה ב-`summary_md` נתמכת ב-`cited_chunk_ids`, בנתוני מזג האוויר או ב-`draft_plan`. אפשר להוסיף במקביל את `faithfulness` של RAGAS להשוואה.
- **Tool Calling Accuracy.** בדיקה דטרמיניסטית מתוך ה-trace: האם נקראו הכלים הצפויים עם הפרמטרים הנכונים (`region`, `date`, `exclude_ids`). מדווחים precision ו-recall לכל צומת.
- **תקינות לולאת הבטיחות.** כל תרחיש מגדיר Mock scenario ותוצאה צפויה. נבדק שמסלול סגור אף פעם לא מופיע ב-`final.plan`, שמספר הניסיונות לא עובר 3, ושתקלה במזג האוויר מסתיימת באזהרה ולא באישור שקט. זו המטריקה החשובה ביותר להצגה.

## 7. סיכונים ונקודות פתוחות

| סיכון | השפעה | מענה |
| --- | --- | --- |
| משקע השיטפון לא נמדד בנקודה: גשם במעלה הנחל מסוכן גם כשבראש השביל יבש | הערכת חסר של סכנה במסלולי נחל | ב-MVP: כלל שמרני למסלולים עם תג `water`/`canyon` בעונת הגשמים, ותרחיש שיטפון ב-Mock |
| איכות נתוני OSM: שמות חסרים, קושי לא מתוייג | סינון שגוי | גזירת קושי מאורך ושיפוע (SRTM כ-Stretch); רשימת עריכה ידנית של כ-30 מסלולי הדגמה |
| תחזית Open-Meteo מוגבלת ל-16 יום קדימה | תאריך רחוק ללא נתונים | `status="out_of_range"` מוביל לאזהרה מפורשת ולהצעה לבדוק שוב קרוב למועד |
| Structured Output דרך OpenRouter נכשל בולידציה | צומת נתקע | `function_calling`, ניסיון חוזר אחד עם הודעת השגיאה, ואז `NodeError` ו-fallback דטרמיניסטי |
| עלות ו-latency של עד 3 לולאות Sonnet | חוויית משתמש | כללים קשיחים קודמים ל-LLM, קאשינג פרומפטים, וסטרימינג של שלבי ביניים כדי שההמתנה תהיה שקופה |

שאלות שכדאי לסגור לפני שלב 1:

- [ ] איזה אזור בוחרים? המסמך מזכיר את נחל כזיב (גליל עליון) ואת נחל אל על (גולן). ההמלצה: bbox אחד של גליל עליון וגולן, שמכוסה בהרבה מסלולי מים.
- [ ] לאמת את ה-slugs המדויקים של Sonnet 5.5 ו-Haiku 5.5 בדף המודלים של OpenRouter, ולעדכן את `.env`.
- [ ] להחליט אם זמן נסיעה נמדד בהערכה גיאומטרית (MVP) או בשרת OSRM מקומי (עוד שירות ב-Compose).
- [ ] להחליט מה מקור הטיפים ל-RAG: כתיבה עצמית או תקצירים ממקורות פתוחים עם רישיון מתאים, כולל שדה `source` לכל chunk.
