# Unity Catalog DDL

Apply in order. Idempotent — every statement is `IF NOT EXISTS`.

```bash
databricks sql query --warehouse-id "$DBX_WAREHOUSE_ID" --file uc_ddl/001_core_tables.sql
databricks sql query --warehouse-id "$DBX_WAREHOUSE_ID" --file uc_ddl/002_model_results.sql
databricks sql query --warehouse-id "$DBX_WAREHOUSE_ID" --file uc_ddl/003_app_volume.sql
```

| File | What | Skipping it costs |
|---|---|---|
| `001_core_tables.sql` | `run_status`, `run_events`, `run_logs`, `run_progress`, `run_results_meta` | Every durable write fails at the end of a run |
| `002_model_results.sql` | One results table per model family | That model's results are lost; its telemetry still lands |
| `003_app_volume.sql` | `app_store`, the app's durable filesystem | `/healthz` reports `volume` degraded; nothing on the run path is affected |

Only 001 is mandatory. 002 costs you a model's results; 003 is genuinely
optional and degrades cleanly — see `resources/app.yml` for the grants that go
with it.

**Neither file has ever been executed.** `databricks bundle deploy` has never
run against a workspace, so a syntax error in here would not have surfaced
yet. Read changes carefully rather than trusting that what is committed works.

Four things worth knowing before changing anything here:

- **Column shapes mirror `shared/tables.py`.** `to_row()` produces exactly
  these keys. Change one, change both — a mismatch surfaces at write time on a
  real workspace and nowhere in the test suite.
- **The per-model tables mirror each model's result rows, and nothing checks
  that.** `tests/deploy/test_bundle.py` proves a table with the right *name*
  exists for every model, in both directions. It does not compare a single
  column. The two failure modes are not symmetric: a column no model writes is
  harmless clutter, but **a row key with no column is a silently dropped
  field**. Diff them by hand when you touch either side — every key in the
  dict a model passes to `emit("result", rows=...)` or returns from
  `results()` needs a column, minus `run_id` and `chunk_index`, which
  `job/emitter.py` stamps and the model must not supply. (Audited across all
  eleven models on 2026-08-25: no mismatches, and no NOT NULL column receives
  a null on any path.)
- **`main.dbx_leaning` is hardcoded here and is `${var.catalog}` /
  `${var.schema}` everywhere else.** These files are applied by hand and
  `databricks sql query --file` does no substitution, so retargeting a
  deployment means editing both files in the same commit that changes the
  variable. See the header of `001_core_tables.sql`.
- **JSON `STRING` columns, not `VARIANT`.** VARIANT support in the Python
  `deltalake` bindings lags the Rust kernel (delta-rs #3637). CLAUDE.md rates
  VARIANT nice-to-have; this is the documented fallback.

## Changing a table that already exists

`CREATE TABLE IF NOT EXISTS` does exactly what it says: it will **not** add a
column to a table that is already there. When these files gain a column — the
provenance columns did, when the models started reading sample data — an
environment where the DDL has already run needs the change applied by hand:

```sql
ALTER TABLE main.dbx_leaning.results_forecasting
  ADD COLUMNS (data_source STRING, data_synthetic BOOLEAN,
               data_rows BIGINT, data_fallback_reason STRING);
```

There is no writer that papers over this. An earlier version of this file said
delta-rs would add the column itself with `schema_mode="merge"`, so the
outcome depended on which writer a deployment happened to select. That is no
longer true and was never a safety net: **Spark is the only durable write
path**, `saveAsTable` append fails on a column the table does not have, and
delta-rs has since been deleted outright — it took a storage URI, not a UC
name, and given a three-part name it wrote to a local directory without
erroring (`job/delta.py`). So the
DDL is the authority unconditionally: keep it ahead of the models, and apply
the `ALTER TABLE` wherever it has already run.

Grants are deliberately not in these files: the per-model results tables exist
separately *so* they can be granted separately, and who should see what is a
decision for whoever owns each model's audience, not a default.
