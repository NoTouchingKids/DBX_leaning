Add a model to `job/models/` and register it everywhere the deploy needs it.
There are eleven today; this is the checklist for the twelfth.

A model that only imports cleanly is not done. Six other files know about a
model, `tests/deploy/` binds five of them together, and skipping any one of
them produces a failure that is either a red test or — worse — a green test
suite and a run that dies on a workspace after consuming one of five
account-wide task slots. The registration section below is not optional
polish; it is most of the work.

Ask the user for:

1. The model's name. It is used verbatim as the directory name, the job file
   name, the environment key, the results-table suffix and the key in
   `DBX_JOB_IDS` — so it must be a valid Python identifier, lowercase, and
   the same string in all six places.
2. A one-line description of what it does and **what makes its telemetry
   shape distinct** from the eleven that exist. If it is not distinct from
   any of them, say so — there may be no reason for a new model rather than
   a config on an existing one. The lineup is a set of *shapes*, not a set of
   techniques: solver gap, training loop, completion sweep, chunked results,
   per-unit outcomes with per-unit failure.

## Before writing code

Read `job/models/README.md` first — it is the duck-typed contract in prose, and
`CONVENTIONS` in `job/loader.py` is the same contract in code. If they
disagree, the code is right. Re-read `docs/message-envelope-spec.md` rather
than working from memory; the envelope is frozen and the model has to fit it,
not the other way round.

**Do not write a `.claude/agents/model-<name>.md` brief.** Five of the eleven
models have one and the last six deliberately do not — see
`docs/parallelization-plan.md`, which records that a brief per model was worth
writing only while the contract was still being discovered, and that once
`job/models/README.md` and this command existed the later models needed none. Add
one only if this model introduces something genuinely unlike the others (a new
dependency class, a new progress mechanism); otherwise the brief is one more
document that can go stale and be obeyed anyway.

## 1. The package

```
job/models/<name>/
    __init__.py     exports build_model
    model.py        the class, with results_table as a class-level literal
    <data>.py       instance building / loading, if it needs any
```

Three things the harness and the tests actually require, as opposed to
prefer:

- A module-level factory on the package: `build_model` (or `create_model` /
  `make_model` / `Model` — first match wins, see `job/loader.py`).
- `results_table = "results_<name>"` as a **plain string literal at class
  level in `job/models/<name>/model.py`**. `tests/deploy/test_bundle.py` reads it
  with a regex rather than importing the model — importing would pull
  gurobipy, torch and ortools into the test process for a string constant. A
  computed or `__init__`-assigned value is invisible to that test and the
  model will read as having no results table at all.
- Heavy imports **inside** `build()`/`run()`, never at module scope. The
  loader introspects packages (`job.loader.describe_object`) and tests collect
  them in environments that do not have every model's libraries.

Provenance is not optional. Use `job/models/_data` and put
`Dataset.describe()`'s four keys (`data_source`, `data_synthetic`,
`data_rows`, `data_fallback_reason`) on **every result row**. A run against
real `samples` rows and one that fell back to the deterministic generator must
stay distinguishable from the results table alone, because logs are droppable
by contract and results are not.

## 2. Registration — all six places

Every one of these was required by hand for `ortools_jobshop` and
`panel_fit`. In dependency order:

| # | File | What to add | Enforced by |
|---|---|---|---|
| 1 | `pyproject.toml` `[project.optional-dependencies]` | An extra holding this model's libraries. Reuse an existing one if the deps are identical (`gurobi_routing` shares `gurobi` with `gurobi_scheduling`). An empty extra is fine and is what `annealing` has. | `test_every_registered_model_has_a_dependency_extra` |
| 2 | `pyproject.toml` `[tool.dbx-leaning.models]` | `<name> = "<extra>"`. This registry is what the requirements exporter and the wheel builder read. | `test_every_model_on_disk_is_in_the_registry` |
| 3 | `deploy/requirements/<name>.txt` | **Generated, never hand-written.** `uv lock` if you touched dependencies, then `uv run python scripts/export_requirements.py`. | `tests/deploy/test_requirements.py` (runs the exporter with `--check`) |
| 4 | `resources/model_<name>.job.yml` | The job. Copy the nearest existing one and change every occurrence of the model name. | six tests in `test_bundle.py` — see below |
| 5 | `resources/app.yml` → `DBX_JOB_IDS` | `"<name>": ${resources.jobs.model_<name>.id}`. This map is the app's allow-list: a model absent from it cannot be triggered however well its job is defined. | `test_the_app_knows_about_every_job` |
| 6 | `uc_ddl/002_model_results.sql` | `CREATE TABLE IF NOT EXISTS main.dbx_leaning.results_<name> (...)` with a column for **every key** the model puts in a result row. | `test_every_model_results_table_exists_in_the_ddl` (table only — the *columns* are checked by nothing, see below) |

### What the job file has to get right

`tests/deploy/test_bundle.py` pins all of this, so a copied-and-half-renamed
file fails loudly rather than deploying wrong:

- `resources.jobs.model_<name>` — the key must match the file name.
- `parameters:` must declare **exactly** `JOB_PARAMETER_NAMES` from
  `app/server/routes/runs.py`, no more and no less. Databricks rejects a `run-now`
  parameter a job has not declared, so drift here breaks every trigger.
- Each declared parameter must also be forwarded to the task as
  `KEY={{job.parameters.KEY}}` — serverless tasks have no `spark_env_vars`,
  so a parameter that is declared but not forwarded means the run starts and
  silently ignores its own configuration.
- `DBX_MODEL` defaults to `models.<name>`.
- `environments[0].environment_key` **is the model name**, not the extra
  name, and the task's `environment_key` matches it.
- `dependencies` is exactly
  `["-r ${workspace.file_path}/deploy/requirements/<name>.txt"]`.
- `queue.enabled: true`, `timeout_seconds > 0`, `max_concurrent_runs: 1`.
  Only `scenario` is allowed above 1 — it is the model that exists to
  exercise fan-out.

### The results table: the one link no test checks

`test_every_model_results_table_exists_in_the_ddl` proves a table with the
right *name* exists. Nothing anywhere compares its **columns** against what
the model emits, and the two failure modes are not symmetric:

- A column the model never writes is harmless clutter.
- **A row key with no column is a silently dropped field.** Spark's
  `saveAsTable` append is the only durable write path (delta-rs raises
  `NotImplementedError` — see `job/delta.py`), and a mismatch surfaces on a
  workspace, inside a job, at the end of a long run.

So diff them by hand before you finish: every key in the dict the model
passes to `emit("result", rows=...)` or returns from `results()` needs a
column. `run_id` and `chunk_index` are stamped by `job/emitter.py` and the
model must not supply them. Keep NOT NULL only for columns the model sets on
every row on every path, including a cancelled run.

`CREATE TABLE IF NOT EXISTS` will **not** add a column to a table that
already exists, so if the DDL has ever been applied anywhere, the change also
needs an `ALTER TABLE ... ADD COLUMNS` by hand — see `uc_ddl/README.md`.

### Not enforced anywhere, and still needed

`app/client/src/lib/models.ts` carries a hand-derived `ModelSpec` per model —
its config fields and its progress-payload shape — and `MODEL_SPECS` is what
the SPA renders a trigger form from. Its own header says there is no test that
will tell you when it drifts. A model missing from it is triggerable by API
and invisible in the UI. It currently covers nine of eleven.

## 3. A new shared dependency is its own step

If the model needs a dependency that is not already in `uv.lock`, treat that
as a sequential change, not part of an otherwise-isolated model track: run
`uv add`, commit the lock, and make sure every other in-progress worktree
picks it up before their next install. Never `pip install` into the venv —
that puts something in the environment `uv.lock` does not describe, which is
exactly what the job cannot reproduce.

## 4. Tests

The model must run and be tested with **no harness or transport code
involved** — construct it, hand it a callback that collects messages, call
`build()` and `run()`. That is the whole point of the duck-typed contract.
Follow the existing `tests/models/` files.

Worth covering specifically, because these are where models have actually
been wrong:

- Cancellation returns the incumbent rather than nothing. Results are not
  best-effort: a cancelled run keeps what it had, with a column saying it was
  cancelled.
- `percent_complete` is `None` where it is genuinely unknowable rather than a
  fabricated number, and whatever it *is* a fraction of is named in the
  payload.
- Every result row has the same key set on every path — a failed unit, an
  empty input, a cancelled run. One results table, one row schema.
- The synthetic-fallback path produces the same columns as the real-data
  path.

## 5. Before you say it is done

```bash
uv run python scripts/export_requirements.py   # regenerate, do not hand-edit
uv run pytest -q
uv run ruff check .
```
