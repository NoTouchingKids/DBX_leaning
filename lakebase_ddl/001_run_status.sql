-- Lakebase (managed Postgres) schema: run state only.
--
-- Applied automatically by app/server/store.py's ensure_schema() at startup, which
-- is idempotent — this file exists so the schema is reviewable in the repo
-- rather than only readable as a Python string, and so a DBA can apply it by
-- hand if startup DDL is ever disallowed.
--
-- Only run_status lives here. Everything append-only and high-volume — logs,
-- progress, events, results — stays in Delta (uc_ddl/). See app/server/store.py for
-- why this one table is different.
--
-- Nothing here is version-sensitive: primary keys, ON CONFLICT, advisory
-- locks and partial indexes are unchanged between PostgreSQL 16 and 18.
--
-- An earlier version of this comment asserted "Lakebase runs PostgreSQL 18".
-- It does not: instances created through the CLI on 2026-08-25 came back
-- `PG_VERSION_16`, including one that passed `pg_version: PG_VERSION_18`,
-- which the create path ignores without erroring. The version is choosable
-- only in the workspace UI, and only at creation — see deploy/README.md.
--
-- So the app does not assert it either. `ensure_schema()` runs
-- `SHOW server_version` on the connection it already has open, and
-- `GET /healthz` reports what the server actually said, under
-- `store.server_version`.

CREATE TABLE IF NOT EXISTS run_status (
    run_id       TEXT PRIMARY KEY,
    job_run_id   TEXT,
    model        TEXT   NOT NULL,
    status       TEXT   NOT NULL,
    detail       TEXT,
    started_ts   BIGINT NOT NULL,
    updated_ts   BIGINT NOT NULL,
    requested_by TEXT
);

-- Partial index: the ceiling check and reconciliation both ask only about
-- runs that have not finished, and finished runs are the overwhelming
-- majority once this has been live for a while.
CREATE INDEX IF NOT EXISTS run_status_active_idx
    ON run_status (updated_ts DESC)
    WHERE status NOT IN ('CANCELLED', 'FAILED', 'INFEASIBLE', 'SUCCEEDED');

CREATE INDEX IF NOT EXISTS run_status_recent_idx ON run_status (updated_ts DESC);
