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

Grants are deliberately not in these files: the per-model results tables exist
separately *so* they can be granted separately, and who should see what is a
decision for whoever owns each model's audience, not a default.
