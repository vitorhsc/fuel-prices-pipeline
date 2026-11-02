-- Idempotent bootstrap, executed at the start of every run.
--   raw        : cleaned copy of the API entities + the price history built by the pipeline
--   analytics  : modelled tables for BI / ad-hoc analysis (rebuilt by the transform step)
--   meta       : pipeline state (high-water marks) and run audit log

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS analytics;
CREATE SCHEMA IF NOT EXISTS meta;

-- ------------------------------------------------------------------ raw
CREATE TABLE IF NOT EXISTS raw.fuel_types (
    fuel_type_id  INTEGER     PRIMARY KEY,
    fuel_type     TEXT        NOT NULL,
    unit          TEXT,
    is_road_fuel  BOOLEAN,
    is_active     BOOLEAN,
    _snapshot_at  TIMESTAMPTZ NOT NULL,
    _run_id       UUID        NOT NULL
);

-- One row per station (SCD type 1: latest attributes win) + when we first/last saw it.
CREATE TABLE IF NOT EXISTS raw.stations (
    station_id     BIGINT      PRIMARY KEY,
    station_name   TEXT,
    brand          TEXT,
    station_type   TEXT,
    district       TEXT,
    municipality   TEXT,
    locality       TEXT,
    address        TEXT,
    postal_code    TEXT,
    latitude       DOUBLE PRECISION,
    longitude      DOUBLE PRECISION,
    first_seen_at  TIMESTAMPTZ NOT NULL,
    last_seen_at   TIMESTAMPTZ NOT NULL,
    _run_id        UUID        NOT NULL
);

-- The API only exposes the CURRENT price. The pipeline builds the history itself:
-- one row per price version, identified by (station, fuel, price_updated_at).
CREATE TABLE IF NOT EXISTS raw.price_history (
    station_id        BIGINT       NOT NULL,
    fuel_type_id      INTEGER      NOT NULL,
    price_updated_at  TIMESTAMPTZ  NOT NULL,  -- when the station changed the price
    price_eur         NUMERIC(6,3) NOT NULL,
    first_seen_at     TIMESTAMPTZ  NOT NULL,  -- when this pipeline first captured it
    last_seen_at      TIMESTAMPTZ  NOT NULL,  -- last snapshot in which it was still current
    _run_id           UUID         NOT NULL,
    PRIMARY KEY (station_id, fuel_type_id, price_updated_at)
);
CREATE INDEX IF NOT EXISTS ix_price_history_fuel_updated
    ON raw.price_history (fuel_type_id, price_updated_at);

-- ------------------------------------------------------------------ meta
CREATE TABLE IF NOT EXISTS meta.pipeline_state (
    dataset         TEXT        NOT NULL,
    scope           TEXT        NOT NULL,
    high_water_mark TIMESTAMPTZ NOT NULL,  -- newest price_updated_at ever captured
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (dataset, scope)
);

CREATE TABLE IF NOT EXISTS meta.pipeline_runs (
    run_id              UUID        NOT NULL,
    dataset             TEXT        NOT NULL,
    scope               TEXT        NOT NULL,
    status              TEXT        NOT NULL CHECK (status IN ('success', 'failed')),
    started_at          TIMESTAMPTZ NOT NULL,
    finished_at         TIMESTAMPTZ NOT NULL,
    rows_extracted      INTEGER     NOT NULL DEFAULT 0,
    rows_rejected       INTEGER     NOT NULL DEFAULT 0,
    new_versions        INTEGER     NOT NULL DEFAULT 0,  -- new price versions inserted
    stations_upserted   INTEGER     NOT NULL DEFAULT 0,
    api_requests        INTEGER     NOT NULL DEFAULT 0,
    high_water_before   TIMESTAMPTZ,
    high_water_after    TIMESTAMPTZ,
    parquet_path        TEXT,
    warning_message     TEXT,
    error_message       TEXT,
    PRIMARY KEY (run_id, dataset, scope)
);
