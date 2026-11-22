-- Spatial layer: hiking trails and points of interest loaded from OpenStreetMap.
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS trails (
    trail_id        TEXT PRIMARY KEY,                 -- e.g. 'osm-relation-123456'
    osm_type        TEXT NOT NULL,
    osm_id          BIGINT NOT NULL,
    name            TEXT NOT NULL,
    name_he         TEXT,
    name_en         TEXT,
    region          TEXT NOT NULL,
    geom            geometry(MultiLineString, 4326) NOT NULL,
    trailhead       geometry(Point, 4326) NOT NULL,
    length_km       DOUBLE PRECISION NOT NULL,
    is_loop         BOOLEAN,
    difficulty      TEXT CHECK (difficulty IN ('easy', 'moderate', 'hard')),
    elevation_gain_m DOUBLE PRECISION,
    tags            TEXT[] NOT NULL DEFAULT '{}',
    osmc_symbol     TEXT,
    colour          TEXT,
    raw_tags        JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (osm_type, osm_id)
);

CREATE INDEX IF NOT EXISTS trails_geom_gix ON trails USING GIST (geom);
CREATE INDEX IF NOT EXISTS trails_trailhead_gix ON trails USING GIST (trailhead);
CREATE INDEX IF NOT EXISTS trails_region_idx ON trails (region);
CREATE INDEX IF NOT EXISTS trails_tags_gin ON trails USING GIN (tags);
CREATE INDEX IF NOT EXISTS trails_name_trgm ON trails USING GIN (name gin_trgm_ops);

CREATE TABLE IF NOT EXISTS pois (
    poi_id      TEXT PRIMARY KEY,                     -- e.g. 'osm-node-987'
    osm_type    TEXT NOT NULL,
    osm_id      BIGINT NOT NULL,
    category    TEXT NOT NULL,                        -- spring, viewpoint, parking, picnic, waterfall, ruins ...
    name        TEXT,
    name_he     TEXT,
    geom        geometry(Point, 4326) NOT NULL,
    raw_tags    JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (osm_type, osm_id)
);

CREATE INDEX IF NOT EXISTS pois_geom_gix ON pois USING GIST (geom);
CREATE INDEX IF NOT EXISTS pois_category_idx ON pois (category);
