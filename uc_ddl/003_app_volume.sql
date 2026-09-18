-- A managed volume for the app's own local storage.
--
-- Everything else in this platform is a table: telemetry the job appends to
-- Delta, results a model writes, run state in Postgres. None of those is a
-- place to put a FILE — an export the user asked for, an uploaded input, a
-- cached artefact too big to hold in memory and too boring to model.
--
-- The app's own filesystem is not that place either. A Databricks App runs up
-- to 24 hours and then stops; its container goes with it, and anything
-- written under /tmp or beside the code is gone. A redeploy does the same.
-- Writing there produces a file the user can download until the moment the
-- app restarts, which is worse than not offering it.
--
-- A volume is the durable filesystem Unity Catalog governs the same way it
-- governs tables. `resources/app.yml` grants the app READ and WRITE on it and
-- passes the path as DBX_APP_VOLUME; `server/config.py` reads that, and
-- `/healthz` reports the volume as degraded rather than fatal when it is
-- absent, because nothing on the run path depends on it.
--
-- MANAGED, not external: Free Edition has no external location to point at,
-- and a managed volume needs no storage credential.
--
-- Like 001 and 002, `main.dbx_leaning` is hardcoded and is a variable
-- everywhere else — see the note at the top of 001_core_tables.sql. If you
-- retarget `var.catalog` or `var.schema`, sed all three files in the same
-- commit.

CREATE SCHEMA IF NOT EXISTS main.dbx_leaning;

CREATE VOLUME IF NOT EXISTS main.dbx_leaning.app_store
  COMMENT 'Durable file storage for the app: exports, uploads, cached artefacts.';

-- The path the app sees. Volumes are mounted read/write at a fixed location,
-- so this is not configurable and not worth deriving:
--
--   /Volumes/main/dbx_leaning/app_store
