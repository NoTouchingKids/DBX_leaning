# Deploying

One Databricks Asset Bundle: **eleven jobs and one app**. Each model is its
own job with its own serverless environment and its own dependency list — the
MCMC job does not carry gurobipy, the ten jobs that need neither torch nor
ortools carry neither, and a model that later needs GPU compute changes
its own file and nothing else.

```
databricks.yml                       the bundle: variables, sync rules, targets
resources/model_<name>.job.yml       one file per model — the microservice boundary
resources/app.yml                    the observer, plus DBX_JOB_IDS wiring
deploy/requirements/<name>.txt       GENERATED per-model deps, exported from uv.lock
requirements.txt                     GENERATED app deps (Databricks Apps reads this path)
entrypoints/run_model.py             what every job actually runs
```

## Before the first deploy

**1. The tables.** Apply the DDL once — `uc_ddl/README.md`.

**2. The ingress secret.** The token a job presents to the app. The app reads
it from a secret; the job never stores it at all — the app passes it per run
as a job parameter at trigger time.

```bash
databricks secrets create-scope dbx-leaning
databricks secrets put-secret dbx-leaning app-token --string-value "$(openssl rand -hex 32)"
```

**3. A warehouse id**, for the app's read path only (backfill and startup
reconciliation). Nothing writes through it — see `docs/architecture.md` on why
the write path bypasses it entirely.

## Deploy

```bash
cd frontend && pnpm install && pnpm build && cd ..   # see below — required
databricks bundle validate                           # schema and references
databricks bundle deploy -t dev
```

**Building the frontend is a required step, not an optional one.** There is no
Node runtime in the workspace, so nothing there will ever build it; the bundle
syncs `frontend/dist` and excludes the source. Skip the build and the deploy
succeeds, the API works, and every page answers 503 with the message in
`app/spa.py::NO_BUNDLE`. `pnpm build` runs `tsc -b` first, so a type error
fails the build rather than shipping a stale `dist/`.

## Lakebase

`run_status` lives in Postgres, not Delta — one row per run, point-looked-up,
and counted against the 5-concurrent-task ceiling. Postgres gives it a primary
key on `run_id` and a transaction around the count-and-claim, so that ceiling
is enforced rather than observed (`app/store.py`, `pg_advisory_xact_lock`).

Point the app at an instance with four variables:

```bash
databricks bundle deploy -t dev \
  --var="lakebase_host=<instance>.database.cloud.databricks.com" \
  --var="lakebase_user=<the app's service principal id>"
  # lakebase_database and lakebase_port have working defaults
```

**Leaving `lakebase_host` empty is supported, and degraded.** The app falls
back to the warehouse-backed store, whose `release_slot` is a documented no-op
that relies on reconciliation to correct it — so the ceiling becomes advisory
rather than transactional, and every check costs warehouse uptime. Nothing
fails; it simply stops being the design in `CLAUDE.md`. `GET /healthz` reports
which store is live and `app/services.py` logs it at startup, which is what
keeps that from being silent.

Apply `lakebase_ddl/001_run_status.sql` before the first run. The app applies
it at startup too, but a deploy that cannot reach the instance reports
`degraded: lakebase` rather than failing, so do not rely on that to tell you
the schema is there.

**The credential is the one piece unverified against a real workspace.**
Lakebase authenticates with a short-lived Databricks OAuth token — which is
why `app/store.py` opens a connection per operation instead of pooling, making
rotation a non-issue. No `DBX_LAKEBASE_PASSWORD` is set in `resources/app.yml`,
so `_lakebase_dsn` falls back to `DATABRICKS_TOKEN`. If that is not present in
the Apps runtime, add the credential as a **secret**, never as a bundle
variable — a variable ends up in the deployment state. The exact YAML is in a
comment in `resources/app.yml` next to the other Lakebase settings, and
`tests/deploy/test_bundle.py` fails if a credential is ever added as a
variable.

## After the first deploy

Then tell the app where it lives, which is only knowable after it has a URL:

```bash
databricks bundle deploy -t dev \
  --var="warehouse_id=<id>" \
  --var="app_public_url=https://<app-host>"
```

Until `app_public_url` is set, jobs run **unobserved** — they complete and
persist normally, and the app backfills from Delta afterwards. That is a
designed state, not a broken one.

## What deploys, and how it gets there

**Code travels by workspace file sync**, not as a wheel. Each job runs
`entrypoints/run_model.py` from the synced tree, which puts the repo root on
`sys.path` and hands the harness its parameters. Moving to a wheel later
changes the task definition and nothing else — the entrypoint contract is the
same either way.

**Dependencies are exported from `uv.lock`**, never re-resolved:

```bash
uv run python scripts/export_requirements.py           # regenerate
uv run python scripts/export_requirements.py --check   # verify (CI-shaped)
```

What deploys is therefore exactly what the tests ran against.
`tests/deploy/test_requirements.py` fails if a generated file drifts from the
lock, if a model's library leaks into another model's environment, or if two
environments end up pinning different versions of a shared dependency.

**The frontend is the exception to "the sync mirrors the repo".** `databricks.yml`
excludes `frontend/src`, `frontend/node_modules` and `frontend/public`, and
names `frontend/dist` under `sync.include` — which is also what gets it past
.gitignore, since a build artifact is correctly ignored by git. The app finds
it at `frontend/dist` relative to the repo root by default
(`app/config.py::frontend_dist`); `DBX_FRONTEND_DIST` overrides that if the
layout ever changes.

## Parameters

Serverless tasks have no `spark_env_vars`, so parameters travel as `KEY=VALUE`
arguments and the entrypoint exports them before the harness reads them.

| Parameter | Meaning |
|---|---|
| `DBX_RUN_ID` | The platform's run id — what a browser subscribes to |
| `DBX_MODEL` | Import spec, e.g. `models.scenario` |
| `DBX_MODEL_CONFIG` | JSON, handed to the model's factory verbatim |
| `DBX_CATALOG` / `DBX_SCHEMA` | Where the tables live |
| `DBX_APP_URL` | Where to attach. Empty = run unobserved |
| `DBX_APP_TOKEN` | Ingress credential, supplied per run by the app |

A Databricks job **rejects a `run-now` parameter it has not declared**, so
every job declares exactly the set the trigger endpoint sends.
`tests/deploy/test_bundle.py` fails if those two drift apart — which would
otherwise surface as every trigger failing at once.

## Concurrency

Free Edition allows **5 concurrent job tasks per account, across all models**.
Databricks has no per-account setting for that, so:

- each job bounds only itself (`max_concurrent_runs`: 1, except `scenario`,
  which exists to exercise fan-out),
- the **app** enforces the account-wide ceiling before triggering and returns
  429 naming the limit (`app/routes/runs.py`),
- jobs `queue` rather than failing if the limit is hit another way.

## Running a job without the app

The app is an optional observer, and this is worth proving on a real
workspace rather than trusting:

```bash
databricks jobs run-now <job-id> --json '{
  "job_parameters": {"DBX_RUN_ID": "manual-1", "DBX_MODEL": "models.scenario"}
}'
```

No `DBX_APP_URL`, so nothing is watching. The run should still complete and
land in `run_logs`, `run_progress`, `run_events` and `results_scenario`.

## Cancelling

From the UI, cancel goes to the app, which forwards it over the job's live
WebSocket. With no live channel there is no path in — the escape hatch is
`databricks jobs cancel-run --run-id <job-run-id>`, which the app's 409 says
in as many words.

## Not done yet

- **No CI.** Nothing runs the test suite on a push; `bundle validate` and the
  `--check` export are both CI-shaped and unwired.
- **`bundle deploy` has never been run** against a real workspace from here.
  The bundle validates against the CLI's schema, and every contract it shares
  with the application code is tested, but validation is not deployment.
