-- Lakebase (managed Postgres) schema: every reported status transition,
-- append-only.
--
-- APPLIED OUT OF BAND, after 001, never by the app or the job starting up.
-- The app's startup check (app/server/store.py::PostgresRunStore.check_schema)
-- reports this table missing or mismatched as `lakebase_schema` degraded on
-- /healthz; it does not create it.
--
--   psql "host=<read_write_dns> port=5432 dbname=databricks_postgres user=<you> sslmode=require" \
--     -v ON_ERROR_STOP=1 -f lakebase_ddl/001_run_status.sql
--   psql "host=<read_write_dns> port=5432 dbname=databricks_postgres user=<you> sslmode=require" \
--     -v ON_ERROR_STOP=1 -f lakebase_ddl/002_run_status_history.sql
--
-- The CREATE SCHEMA is repeated from 001 rather than assumed: it is
-- idempotent, and it means this file applied alone lands somewhere valid
-- instead of erroring — or being "fixed" by dropping the qualification and
-- landing in `public`.
--
-- ---------------------------------------------------------------------------
-- WHY THIS IS A SECOND TABLE, AND NOT `run_status` MADE APPEND-ONLY
--
-- The primary key on run_status.run_id is what makes the current-state upsert
-- possible at all: many rows per run and `ON CONFLICT (run_id)` has nothing to
-- conflict on. So current state keeps its shape and history gets its own
-- table, at the cost of one extra INSERT per transition — issued in the SAME
-- statement as the upsert (a data-modifying CTE; see store.py's REPORT_SQL),
-- so the two cannot disagree about the transition they describe.
--
-- History records what was REPORTED, including a transition the current-row
-- upsert refused because its seq was older than the row's. That reads like a
-- bug and is the point: the two tables answer different questions. Current
-- state is what is true; history is what arrived, and that a stale report
-- arrived at all is the fact you want when working out why the row looks the
-- way it does.
--
-- This is NOT the durable record either. The job's part files on the
-- telemetry volume carry the same transitions and remain the record of truth;
-- this copy exists because reading them means a job or a warehouse, and a few
-- rows per run in Postgres is a point lookup.
-- ---------------------------------------------------------------------------
--
-- UNIQUE (run_id, seq): a status message is identified by its run and seq, so
-- a redelivered report — a reconnect, a retry — inserts with
-- `ON CONFLICT (run_id, seq) DO NOTHING` and stays one row. It is a full, not
-- partial, constraint because every writer has a seq: a transition that did
-- not come from an envelope message has no business in this table. The index
-- it creates also serves the per-run read, `WHERE run_id = ? ORDER BY seq`.
--
-- `id` is BIGSERIAL for insertion order, which is the tiebreak `ts` cannot
-- give: `ts` is epoch milliseconds, and QUEUED then RUNNING inside one
-- millisecond is an ordinary fast start.
--
-- `seq` and `ts` are BIGINT, not TEXT: compared as strings, "2" > "12".

CREATE SCHEMA IF NOT EXISTS dbx_leaning;

CREATE TABLE IF NOT EXISTS dbx_leaning.run_status_history (
    id        BIGSERIAL PRIMARY KEY,
    run_id    TEXT    NOT NULL,
    seq       BIGINT  NOT NULL,
    status    TEXT    NOT NULL,
    terminal  BOOLEAN NOT NULL,
    detail    TEXT,
    ts        BIGINT  NOT NULL,
    CONSTRAINT run_status_history_run_seq_key UNIQUE (run_id, seq)
);
