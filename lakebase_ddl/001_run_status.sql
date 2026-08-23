-- Lakebase (managed Postgres) schema: run state only.
--
-- Applied automatically by app/store.py's ensure_schema() at startup, which
-- is idempotent — this file exists so the schema is reviewable in the repo
-- rather than only readable as a Python string, and so a DBA can apply it by
-- hand if startup DDL is ever disallowed.
--
-- Only run_status lives here. Everything append-only and high-volume — logs,
-- progress, events, results — stays in Delta (uc_ddl/). See app/store.py for
-- why this one table is different.
--
-- Lakebase runs PostgreSQL 18. This was developed and tested against 16,
-- which is what the development environment provides; nothing used here
-- (primary keys, ON CONFLICT, advisory locks, partial indexes) changed
-- between those versions, but that is a statement about the feature set, not
-- a test result.

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
