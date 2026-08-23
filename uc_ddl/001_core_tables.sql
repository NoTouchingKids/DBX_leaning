-- Core platform tables.
--
-- Shapes match shared/tables.py's row projection exactly; if you change one,
-- change both. Nested fields are JSON STRING columns rather than VARIANT:
-- VARIANT support in the Python deltalake bindings lags the Rust kernel
-- (delta-rs #3637), and CLAUDE.md rates VARIANT nice-to-have, not required.
--
-- Everything except run_status is append-only, written by the job through
-- Delta. The SQL warehouse never sees a write on the telemetry path — its
-- cost is driven by uptime, so the write path deliberately bypasses it.

CREATE CATALOG IF NOT EXISTS main;
CREATE SCHEMA IF NOT EXISTS main.dbx_leaning;

-- Current state, one row per run. The ONLY table the app mutates, and it does
-- so on a lifecycle transition — never on a timer.
CREATE TABLE IF NOT EXISTS main.dbx_leaning.run_status (
    run_id       STRING  NOT NULL,
    job_run_id   STRING,            -- Databricks' own run id, for reconciliation
    model        STRING,
    status       STRING  NOT NULL,  -- QUEUED|RUNNING|SUCCEEDED|FAILED|CANCELLED|INFEASIBLE
    detail       STRING,
    started_ts   BIGINT,            -- epoch ms
    updated_ts   BIGINT  NOT NULL,
    requested_by STRING
)
USING DELTA
COMMENT 'Current state per run. Updated by the app; never polled on a timer.';

-- Append-only status transitions written by the JOB. Distinct from run_status:
-- a job that runs while the app is down still records its transitions here,
-- and startup reconciliation reads them back.
CREATE TABLE IF NOT EXISTS main.dbx_leaning.run_events (
    run_id STRING NOT NULL,
    seq    BIGINT NOT NULL,
    ts     BIGINT NOT NULL,
    status STRING NOT NULL,
    detail STRING
)
USING DELTA
COMMENT 'Append-only lifecycle transitions, written by the job.';

CREATE TABLE IF NOT EXISTS main.dbx_leaning.run_logs (
    run_id         STRING  NOT NULL,
    seq            BIGINT  NOT NULL,
    ts             BIGINT  NOT NULL,
    level          STRING  NOT NULL,  -- DEBUG|INFO|WARNING|ERROR
    source         STRING  NOT NULL,  -- 'gurobi' | 'model' | 'job'
    phase          STRING  NOT NULL,  -- 'input' | 'build' | 'solve' | 'results'
    message        STRING  NOT NULL,
    -- Filters the LIVE send only. Everything is stored regardless.
    client_visible BOOLEAN NOT NULL
)
USING DELTA
COMMENT 'Best-effort live, never dropped durably.';

CREATE TABLE IF NOT EXISTS main.dbx_leaning.run_progress (
    run_id               STRING NOT NULL,
    seq                  BIGINT NOT NULL,
    ts                   BIGINT NOT NULL,
    elapsed_seconds      DOUBLE NOT NULL,
    percent_complete     DOUBLE,          -- null when genuinely unknowable (MIP)
    primary_metric       DOUBLE,
    primary_metric_label STRING,
    payload_json         STRING           -- model-specific extras
)
USING DELTA
COMMENT 'Sampled progress points. Never one row per solver iteration.';

-- Result METADATA. The rows themselves live in each model's own table, under
-- its own grants, because different models serve different audiences.
CREATE TABLE IF NOT EXISTS main.dbx_leaning.run_results_meta (
    run_id          STRING  NOT NULL,
    seq             BIGINT  NOT NULL,
    ts              BIGINT  NOT NULL,
    chunk_index     INT     NOT NULL,  -- 0 for once-at-the-end models
    final           BOOLEAN NOT NULL,  -- false while more chunks are coming
    -- Rows ACTUALLY written. This is what distinguishes "succeeded, wrote
    -- 8,760 rows" from "succeeded, wrote nothing because the write failed".
    row_count       BIGINT  NOT NULL,
    fetch_hint_json STRING,
    preview_json    STRING             -- bounded, LTTB-downsampled
)
USING DELTA
COMMENT 'Result summaries and pointers. Never the result set itself.';
