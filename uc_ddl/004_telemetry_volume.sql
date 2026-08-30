-- The telemetry volume: where a run writes its own record, as it happens.
--
-- This is v4's durable path (docs/v4-rewrite-plan.md). The job writes each
-- envelope through to a file here as it is produced — no in-memory buffer, no
-- Spark, no Delta on the telemetry path at all. A separate scheduled or
-- streaming ingestion job reads these files and loads them into SQL.
--
-- Layout, one directory per run:
--
--   /Volumes/main/dbx_leaning/telemetry/runs/<run_id>/part-00001.jsonl
--
-- Each run owns its own directory, so concurrent runs cannot conflict — which
-- is strictly simpler than Delta's optimistic concurrency and its S3 locking
-- caveat. Whether a run writes ONE growing file or a series of closed part
-- files is what `scripts/probe_volume_append.py` decides; the layout above
-- accommodates either.
--
-- ---------------------------------------------------------------------------
-- THE GRANT IS THE ARCHITECTURE. Read this before "helpfully" adding the app.
--
-- The app gets NOTHING on this volume, deliberately. v4's rule is that the app
-- never reads run telemetry from files: a live gap is filled by asking the JOB
-- to replay it from its own log over the WebSocket, and history for a finished
-- run comes from SQL after ingestion. Both paths exist and neither needs this
-- volume.
--
-- Granting the app READ here would not break anything today. It would quietly
-- turn a boundary into a suggestion, and the shortcut it enables — "just read
-- the files, it's right there" — is the one that makes replay dead code and
-- couples the app to the durable format. The permission error IS the design
-- working.
--
-- So:  app  -> no grant, and not even the path in its config
--      job  -> READ + WRITE   (READ because replay reads back what it wrote)
--      ingestion -> READ
--
-- Note that READ and WRITE are separate privileges: WRITE VOLUME does not
-- imply READ VOLUME. The app's own volume in 003 grants both explicitly for
-- the same reason.
--
-- ---------------------------------------------------------------------------
-- `main.dbx_leaning` is HARDCODED here and is a variable everywhere else —
-- the same trap 001 documents at length. `databricks sql query --file` does no
-- variable substitution, so if you retarget `var.catalog` / `var.schema`, sed
-- 001, 002, 003 and this file in the same commit. Delta will not save you: the
-- volume is created in one place, the job writes to another, and the failure
-- arrives inside a run on a workspace.
--
-- Likewise the PRINCIPALS below are placeholders. There is no substitution
-- here either, so fill in the service principal the jobs actually run as
-- before applying, or the GRANTs fail with a principal that does not exist.

CREATE SCHEMA IF NOT EXISTS main.dbx_leaning;

CREATE VOLUME IF NOT EXISTS main.dbx_leaning.telemetry
  COMMENT 'Run telemetry, written by the job as it happens. Job-only: the app has no grant here by design — see uc_ddl/004_telemetry_volume.sql.';

-- --------------------------------------------------------------------------
-- Grants. Replace the placeholders with real principals before applying.
--
-- `<job-service-principal>` is whoever the model jobs run as — the
-- `run_as` in databricks.yml, or the deploying user if none is set.
-- `<ingestion-service-principal>` may well be the same one to begin with; keep
-- the statements separate anyway, so splitting them later is an edit rather
-- than an archaeology exercise.
-- --------------------------------------------------------------------------

GRANT READ VOLUME, WRITE VOLUME
  ON VOLUME main.dbx_leaning.telemetry
  TO `<job-service-principal>`;

GRANT READ VOLUME
  ON VOLUME main.dbx_leaning.telemetry
  TO `<ingestion-service-principal>`;

-- The path the job sees. Volumes mount read/write at a fixed location, so this
-- is not configurable and not worth deriving:
--
--   /Volumes/main/dbx_leaning/telemetry
--
-- The job reads it from DBX_TELEMETRY_VOLUME. The app has no equivalent
-- variable, and adding one is the first symptom of the boundary above eroding.
