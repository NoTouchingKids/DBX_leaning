-- Lakebase (managed Postgres) schema: the append-only transition log.
--
-- Applied automatically by app/server/store.py's ensure_schema() at startup,
-- in the same statement string as 001 — Postgres runs a multi-statement query
-- as one implicit transaction, so the two tables arrive together or not at
-- all. Committed here for the same reasons 001 is: reviewable as SQL rather
-- than only as a Python string, and appliable by hand if startup DDL is ever
-- disallowed. Keep this file and store.py's history_schema_sql() in step;
-- tests/deploy/test_lakebase_ddl.py fails the moment they differ.
--
-- Applying by hand means applying BOTH files. `deploy/README.md`'s psql
-- invocation names 001 on its own, which is why the CREATE SCHEMA below is
-- repeated rather than assumed — it is idempotent, and it means this file
-- applied alone still lands somewhere valid instead of erroring on a missing
-- schema (or worse, being "fixed" by dropping the qualification and landing
-- in `public`).
--
-- ---------------------------------------------------------------------------
-- WHY THIS IS A SECOND TABLE, AND NOT `run_status` MADE APPEND-ONLY
--
-- The obvious simplification is to stop overwriting the current-state row and
-- keep every transition in it instead. Do not. `run_status` is in Postgres
-- rather than Delta for exactly two properties, and appending kills both:
--
--   1. The PRIMARY KEY on run_id. Many rows per run and there is nothing left
--      to conflict on, so `ON CONFLICT (run_id) DO UPDATE` has no target —
--      that is the job's upsert in job/lakebase.py, `updated_ts` guard and
--      all, plus this store's set_status. A duplicate run_id also stops being
--      refusable, which is the precise Delta failure the move to Postgres
--      fixed: two registry rows for one run, and the reader taking whichever
--      came back first.
--
--   2. The transactional count-and-claim. claim_slot counts non-terminal runs
--      inside one advisory-locked transaction, and that count is what makes
--      the account's 5-concurrent-task ceiling real rather than advisory.
--      Against an append-only table it becomes a latest-row-per-run subquery.
--      Written slightly wrong it over-counts finished runs and jams the
--      ceiling shut, or under-counts and sails straight past it — and neither
--      mistake announces itself.
--
-- So: current state keeps its shape, history gets its own table. The cost of
-- the split is one extra INSERT per transition. The cost of merging them is
-- the two things this database was chosen for.
--
-- This is NOT the durable record. Delta's run_events
-- (uc_ddl/001_core_tables.sql) holds the same transitions and remains the
-- record of truth; the job writes both. This copy exists because reading
-- run_events means waking the SQL warehouse, whose cost is uptime — the exact
-- spend the platform is built to avoid. A few rows per run is not the
-- telemetry volume that belongs in Delta; logs, progress and results still do.
-- ---------------------------------------------------------------------------
--
-- `id` is BIGSERIAL because `ts` is epoch milliseconds and milliseconds
-- collide: QUEUED then RUNNING inside one millisecond is an ordinary fast
-- start, and two transitions read back in the wrong order are a history that
-- lies. Insertion order breaks the tie.
--
-- `seq` and `ts` are BIGINT, not TEXT. Compared as strings, "2" > "12" — the
-- bug this repo hit twice on the warehouse side, and the reason every
-- parameter it binds is typed.

CREATE SCHEMA IF NOT EXISTS dbx_leaning;

CREATE TABLE IF NOT EXISTS dbx_leaning.run_status_history (
    id          BIGSERIAL PRIMARY KEY,
    run_id      TEXT   NOT NULL,
    seq         BIGINT,            -- the envelope seq of the status message, when it came from one
    status      TEXT   NOT NULL,
    detail      TEXT,
    ts          BIGINT NOT NULL,   -- epoch ms, when the transition happened
    recorded_by TEXT   NOT NULL DEFAULT 'job'
);

-- Retries are idempotent: the job may report the same status message twice
-- after a reconnect, and `seq` identifies it uniquely within a run. Partial,
-- because a writer with no envelope message behind it (the app, at slot-claim
-- time) has no seq and must still be able to append.
CREATE UNIQUE INDEX IF NOT EXISTS run_status_history_seq_idx
    ON dbx_leaning.run_status_history (run_id, seq) WHERE seq IS NOT NULL;

CREATE INDEX IF NOT EXISTS run_status_history_run_idx
    ON dbx_leaning.run_status_history (run_id, ts DESC);
