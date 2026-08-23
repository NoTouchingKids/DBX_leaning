# Unity Catalog DDL

Apply in order. Idempotent — every statement is `IF NOT EXISTS`.

```bash
databricks sql query --warehouse-id "$DBX_WAREHOUSE_ID" --file uc_ddl/001_core_tables.sql
databricks sql query --warehouse-id "$DBX_WAREHOUSE_ID" --file uc_ddl/002_model_results.sql
```

| File | What |
|---|---|
| `001_core_tables.sql` | `run_status`, `run_events`, `run_logs`, `run_progress`, `run_results_meta` |
| `002_model_results.sql` | One results table per model family |

Two things worth knowing before changing anything here:

- **Column shapes mirror `shared/tables.py`.** `to_row()` produces exactly
  these keys. Change one, change both — a mismatch surfaces at write time on a
  real workspace and nowhere in the test suite.
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

This matters more for the Spark writer than for delta-rs: delta-rs writes with
`schema_mode="merge"` and would add the column itself, while Spark's
`saveAsTable` append fails on a column the table does not have. Since the
writer is chosen at startup by what is importable, the same code can succeed on
one deployment and fail on another — so treat the DDL as the authority and keep
it ahead of the models.

Grants are deliberately not in these files: the per-model results tables exist
separately *so* they can be granted separately, and who should see what is a
decision for whoever owns each model's audience, not a default.
