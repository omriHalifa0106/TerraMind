-- Data pipeline: weather forecasts and trail alerts, ingested on a schedule by ingestion/pipeline.py.
-- The weather and alerts MCP servers read these tables; agents never touch them directly.

-- One row per pipeline run: what ran, when, how it ended. The audit trail of the pipeline.
CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id       BIGSERIAL PRIMARY KEY,
    source       TEXT NOT NULL CHECK (source IN ('weather', 'alerts')),
    trigger      TEXT NOT NULL CHECK (trigger IN ('schedule', 'manual', 'scenario', 'test')),
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ,
    status       TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'ok', 'partial', 'failed')),
    rows_written INTEGER NOT NULL DEFAULT 0,
    detail       TEXT
);
CREATE INDEX IF NOT EXISTS ingestion_runs_latest_idx ON ingestion_runs (source, finished_at DESC);

-- Daily forecast per ~1 km grid cell (trailheads rounded to 2 decimals) and date (Open-Meteo).
CREATE TABLE IF NOT EXISTS weather_forecasts (
    lat_key             NUMERIC(5, 2) NOT NULL,
    lon_key             NUMERIC(5, 2) NOT NULL,
    for_date            DATE NOT NULL,
    temp_max_c          REAL,
    apparent_temp_max_c REAL,
    precip_prob_max_pct SMALLINT,
    precip_sum_mm       REAL,
    wind_gust_max_kmh   REAL,
    uv_index_max        REAL,
    sunrise             TIMESTAMP,                      -- local time (Asia/Jerusalem), as Open-Meteo returns it
    sunset              TIMESTAMP,
    hourly_precip_prob  JSONB NOT NULL DEFAULT '{}'::jsonb,  -- {"hour": probability %}
    fetched_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_id              BIGINT REFERENCES ingestion_runs (run_id) ON DELETE SET NULL,
    PRIMARY KEY (lat_key, lon_key, for_date)
);

-- Current status of every trail: open or closed, and expected crowding.
CREATE TABLE IF NOT EXISTS trail_status (
    trail_id    TEXT PRIMARY KEY REFERENCES trails (trail_id) ON DELETE CASCADE,
    is_open     BOOLEAN NOT NULL,
    reason      TEXT,
    crowd_level TEXT NOT NULL CHECK (crowd_level IN ('low', 'medium', 'high', 'unknown')),
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_id      BIGINT REFERENCES ingestion_runs (run_id) ON DELETE SET NULL
);

-- Active alerts. trail_id NULL = applies to a whole region (or everywhere when region is NULL too).
CREATE TABLE IF NOT EXISTS trail_alerts (
    id         BIGSERIAL PRIMARY KEY,
    alert_id   TEXT NOT NULL,
    trail_id   TEXT REFERENCES trails (trail_id) ON DELETE CASCADE,
    region     TEXT,                                     -- region key (terramind.regions)
    kind       TEXT NOT NULL,
    severity   TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    message    TEXT NOT NULL,
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to   TIMESTAMPTZ,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_id     BIGINT REFERENCES ingestion_runs (run_id) ON DELETE SET NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS trail_alerts_unique_idx ON trail_alerts (alert_id, COALESCE(trail_id, ''));
CREATE INDEX IF NOT EXISTS trail_alerts_trail_idx ON trail_alerts (trail_id);
