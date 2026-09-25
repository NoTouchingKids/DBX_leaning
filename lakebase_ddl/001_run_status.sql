-- Lakebase (managed Postgres) schema: current run state, one row per run.
--
-- APPLIED OUT OF BAND, BY A HUMAN OR A DEPLOY STEP — never by the app or the
-- job starting up. Neither process creates or alters anything here: the app
-- CHECKS at startup that this table exists with the columns it expects
-- (app/server/store.py::PostgresRunStore.check_schema) and, if not, reports
-- `lakebase_schema` degraded on /healthz with the reason, rather than creating
-- it. See docs/v5-implementation-plan.md, Phase 2 item 3.
--
-- Apply both files, in order, as a role that may create in the database —
-- the instance owner, normally (see deploy/README.md for the grants the app's
-- service principal then needs):
--
--   psql "host=<read_write_dns> port=5432 dbname=databricks_postgres user=<you> sslmode=require" \
--     -v ON_ERROR_STOP=1 -f lakebase_ddl/001_run_status.sql
--   psql "host=<read_write_dns> port=5432 dbname=databricks_postgres user=<you> sslmode=require" \
--     -v ON_ERROR_STOP=1 -f lakebase_ddl/002_run_status_history.sql
--
-- Both are idempotent (IF NOT EXISTS throughout), so re-applying is safe. They
-- do NOT migrate an older shape: CREATE TABLE IF NOT EXISTS leaves an existing
-- table untouched, and the app's startup check then reports the mismatch.
-- deploy/README.md says how to replace a pre-v5 table.
--
-- This file is canonical. store.py holds no copy of the DDL — only the column
-- names and types it checks for (EXPECTED_COLUMNS), and
-- tests/deploy/test_lakebase_ddl.py fails if those drift from this file.
--
-- Only run state lives in Postgres. Everything append-only and high-volume —
-- logs, progress, events, results — stays in Delta (uc_ddl/). This row is a
-- point-lookup mirror of the run's status messages, not the record of truth:
-- the job's part files on the telemetry volume are.
--
-- NOT `public`, and that is not tidiness. Since PostgreSQL 15 the `public`
-- schema no longer grants CREATE to `PUBLIC`, so a role that does not own the
-- database gets `permission denied for schema public`. The schema name is
-- DBX_LAKEBASE_SCHEMA, defaulting to `dbx_leaning`; keep this file and
-- store.py's DEFAULT_SCHEMA in step. A deployment on a different schema name
-- applies these files with that name substituted.
--
-- Every statement in store.py qualifies the table rather than setting a
-- search_path. A search path is per-session state, and the store opens a
-- connection per operation; one that silently reverted to `public` would find
-- a different, empty table instead of failing.
--
-- Nothing here is version-sensitive: primary keys, ON CONFLICT ... WHERE,
-- data-modifying CTEs and partial indexes are unchanged between PostgreSQL 16
-- and 18. An earlier version of this comment asserted "Lakebase runs
-- PostgreSQL 18"; instances created through the CLI on 2026-08-25 came back
-- `PG_VERSION_16`, including one that passed `pg_version: PG_VERSION_18`. So
-- the app asserts neither: its startup check runs `SHOW server_version`, and
-- `GET /healthz` reports what the server said under `store.server_version`.
--
-- Columns:
--
--   status      free text. The platform's six are conventions, not a CHECK:
--               a model-defined status (e.g. a solver's INFEASIBLE) is legal.
--   terminal    STORED, as the producer stated it in StatusMessage.terminal —
--               never derived from a list of status strings, which cannot
--               answer for a model-defined one.
--   seq         the envelope seq of the status message this row reflects.
--               Job-assigned, per run, monotonic. It is the upsert's guard: a
--               write applies only when its seq >= the row's, so a late or
--               redelivered report cannot move the row backwards. Never a
--               wall clock — a late write always carries the later now().
--   updated_at  epoch MILLISECONDS, like the envelope's `ts`, and taken from
--               the message rather than the database clock.
--   model, job_run_id
--               a later write never blanks a known value (NULLIF/COALESCE in
--               the upsert), so a writer that does not know them — the app,
--               today — cannot erase what the job reported.
--
-- There is no `started_at`: nothing lists or sorts by it, and a run's first
-- reported transition is in run_status_history (002). There is no
-- `requested_by`: nothing ever wrote it.
--
-- BIGINT, not TEXT, for seq and updated_at: compared as strings "2" > "12",
-- the bug this repo hit twice on the warehouse side.

CREATE SCHEMA IF NOT EXISTS dbx_leaning;

CREATE TABLE IF NOT EXISTS dbx_leaning.run_status (
    run_id      TEXT    PRIMARY KEY,
    job_run_id  TEXT,
    model       TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL,
    terminal    BOOLEAN NOT NULL,
    detail      TEXT,
    seq         BIGINT  NOT NULL,
    updated_at  BIGINT  NOT NULL
);

-- The listing: newest first (GET /api/runs).
CREATE INDEX IF NOT EXISTS run_status_recent_idx
    ON dbx_leaning.run_status (updated_at DESC);

-- Runs that have not finished, by the stored `terminal` flag. Finished runs
-- are the overwhelming majority once this has been live a while, so a
-- "what is still running" read stays cheap. Nothing issues that read yet;
-- the index is here so the first one that does is not a sequential scan.
CREATE INDEX IF NOT EXISTS run_status_active_idx
    ON dbx_leaning.run_status (updated_at DESC)
    WHERE terminal = false;
