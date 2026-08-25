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
--
-- ---------------------------------------------------------------------------
-- `main.dbx_leaning` is HARDCODED here, and it is a variable everywhere else.
--
-- The bundle declares `var.catalog` / `var.schema` (defaulting to exactly
-- these two values), passes them to every job as DBX_CATALOG/DBX_SCHEMA and
-- to the app as env, and `shared/tables.py::TableSet` qualifies every write
-- with whatever those say. These files are applied by hand —
-- `databricks sql query --file` does no variable substitution — so nothing
-- ties the two together.
--
-- Consequence: change either bundle variable and the tables get created in
-- one place while every job writes to another. Delta will not save you; the
-- write simply fails with TABLE_OR_VIEW_NOT_FOUND on a workspace, inside a
-- job, at the end of a run. If you retarget a deployment, sed this file and
-- 002 to match the same commit that changes the variable.
--
-- The CREATE CATALOG below is here for completeness and is a no-op on Free
-- Edition, where `main` is pre-provisioned. It will not help a retargeted
-- deployment: it names `main` literally too.
-- ---------------------------------------------------------------------------

CREATE CATALOG IF NOT EXISTS main;
CREATE SCHEMA IF NOT EXISTS main.dbx_leaning;

-- Current state, one row per run. The ONLY table the app mutates, and it does
-- so on a lifecycle transition — never on a timer.
--
-- NOTE: this table is the FALLBACK home for run state. When Lakebase is
-- configured (DBX_LAKEBASE_*), run_status lives in Postgres instead — see
-- lakebase_ddl/001_run_status.sql and app/store.py. It is OLTP-shaped
-- (one row per run, updated constantly, point-looked-up, counted against a
-- ceiling), which Delta is poor at and which costs warehouse uptime to read.
-- This definition stays so a deployment works before Lakebase is provisioned,
-- with two known limitations the Postgres version does not have: no primary
-- key on run_id, and no transaction around the concurrency check.
--
-- Do NOT "tidy" this to match lakebase_ddl/001_run_status.sql. `model` and
-- `started_ts` are NOT NULL there and deliberately nullable here, because the
-- two stores reach this table by different routes: Postgres always inserts
-- through claim_slot, which has both values, while the warehouse path also
-- upserts through app/repository.py's MERGE, whose NOT MATCHED branch supplies
-- only (run_id, job_run_id, status, detail, updated_ts). Adding NOT NULL to
-- either column would turn that branch into a hard failure.
--
-- `job_run_id` is populated only on the Postgres path. The MERGE's MATCHED
-- branch does not assign it and the row always exists by the time
-- attach_job_run runs, so on the warehouse store this column stays NULL and
-- startup reconciliation loses its Jobs-API route (app/reconcile.py falls back
-- to the latest run_events row). That is an app/repository.py bug, recorded
-- here because this is where someone reading the schema will wonder why the
-- column is always empty.
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
