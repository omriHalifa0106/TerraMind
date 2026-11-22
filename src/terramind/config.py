"""Central configuration. Every tunable value comes from the environment or `.env`."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# (south, west, north, east) in WGS84. Upper Galilee + Golan Heights.
DEFAULT_BBOX: tuple[float, float, float, float] = (32.70, 35.10, 33.33, 35.90)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- app ---
    app_url: str = "http://localhost:8000"
    environment: str = "dev"

    # --- database ---
    database_url: str = "postgresql://terramind:terramind@localhost:5433/terramind"
    db_pool_min: int = 1
    db_pool_max: int = 10
    db_statement_timeout_ms: int = 5000

    # --- LLM: Anthropic API, directly or via an Anthropic-compatible gateway ---
    anthropic_base_url: str | None = None  # ANTHROPIC_BASE_URL, without /v1
    anthropic_auth_token: str = ""  # ANTHROPIC_AUTH_TOKEN (sent as Bearer)
    anthropic_api_key: str = ""  # ANTHROPIC_API_KEY (sent as x-api-key)
    anthropic_model_smart: str = "claude-sonnet-5-5"
    anthropic_model_fast: str = "claude-haiku-5-5"
    anthropic_max_tokens: int = 4096
    llm_timeout_s: float = 45.0
    llm_max_retries: int = 2

    # --- embeddings ---
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = 1024
    # "bge" = real sentence-transformers model; "hash" = deterministic offline embedder (tests / no torch)
    embedding_backend: str = "bge"

    # --- MCP servers (streamable HTTP) ---
    mcp_gis_url: str = "http://localhost:8101/mcp"
    mcp_knowledge_url: str = "http://localhost:8102/mcp"
    mcp_weather_url: str = "http://localhost:8103/mcp"
    mcp_alerts_url: str = "http://localhost:8104/mcp"
    mcp_tool_timeout_s: float = 20.0
    # Where the MCP servers listen. Local only by default; docker-compose sets 0.0.0.0 inside containers.
    mcp_bind_host: str = "127.0.0.1"

    # --- external APIs ---
    open_meteo_url: str = "https://api.open-meteo.com/v1/forecast"
    # Public Overpass instances, tried in order; the main one is often overloaded (502/504).
    overpass_urls: list[str] = Field(
        default_factory=lambda: [
            "https://overpass-api.de/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter",
            "https://overpass.private.coffee/api/interpreter",
        ]
    )
    mock_alerts_url: str = "http://localhost:8200"
    http_timeout_s: float = 8.0

    # --- ingestion ---
    region_name: str = "Upper Galilee & Golan"
    region_bbox: tuple[float, float, float, float] = DEFAULT_BBOX

    # --- graph limits ---
    max_search_rounds: int = 2  # agent 2 runs again (relaxed) when agent 3 vetoes every candidate
    candidates_to_check: int = 5  # agent 3 checks this many of agent 2's best candidates
    recursion_limit: int = 25


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
