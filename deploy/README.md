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

## Layout: `app/` is the whole app

The shape the [Databricks app template][t] uses — `server/` for the FastAPI
code, `client/` for the React source, `requirements.txt` at the app root —
one level down, so the repo can hold the jobs too:

[t]: https://github.com/databricks-solutions/claude-databricks-app-template

```
DBX_leaning/
├── app/                 <- source_code_path. Nothing outside it deploys.
│   ├── server/          the FastAPI package: main.py, routes/, spa.py
│   ├── client/          the React source. Never deployed.
│   │   ├── index.html
│   │   ├── vite.config.ts       build.outDir: "../dist"
│   │   └── src/
│   ├── dist/            the built SPA. Committed. server/spa.py serves it.
│   ├── shared/          a TRACKED COPY of ../shared — see below
│   └── requirements.txt where Databricks Apps looks for it
├── shared/              canonical. job/, models/ and tests import this one.
├── job/  models/  entrypoints/      the jobs. The app never imports them.
└── databricks.yml  resources/       the bundle
```

Three consequences, all of which have bitten this repo:

**`app/shared/` is a copy, and copies drift.** `app/` deploys alone, so
everything `server/` imports has to be inside it — and `server/` imports
exactly one first-party package, `shared`. It cannot be a symlink (see below).
So one directory is canonical and one is a copy:

```bash
uv run python scripts/sync_shared.py           # refresh it
uv run python scripts/sync_shared.py --check    # is it current?
```

`tests/deploy/test_shared_copy.py` fails the moment the two differ, and also
asserts that tests import the canonical `shared/` rather than the copy —
because the nasty version of this bug is tests passing against one copy while
the deployed app runs the other. `pythonpath` in `pyproject.toml` puts the
repo root ahead of `app/` for exactly that reason. The duplication is scoped
to this stage; packaging `shared` as a wheel retires it.

**`app/dist/` is committed.** Build output in git is unusual. It is here
because a deploy driven from *inside* Databricks — a Git folder, a notebook —
has no Node runtime and sees only tracked files, so a gitignored bundle would
simply not be there. The cost: **rebuild and commit it whenever the client
changes**, or the deployed UI is silently stale. Sourcemaps stay out of git —
5.1 MB against 1.2 MB for the bundle.

**No symlink may reach the workspace.** The App deployment exports its
`source_code_path` folder and the export rejects symlinks, naming one file:

```
Failed to export .../DBX_leaning/.venv/bin/python
INVALID_PARAMETER_VALUE: Path (...) is not an exportable asset. type=symlink
```

`.venv` is what fails first and is not the whole problem —
`app/client/node_modules` holds thousands, that being how pnpm stores
packages. Both are in `databricks.yml`'s `sync.exclude`, and
`tests/deploy/test_bundle.py` asserts it.

**A sync exclude does not clean up what an earlier deploy already uploaded.**
If a previous run put `.venv` in the workspace, delete it there once:

```bash
databricks workspace delete /Workspace/Users/<you>/DBX_leaning/.venv --recursive
```

## The app's volume

The app gets a Unity Catalog volume for durable file storage —
`uc_ddl/003_app_volume.sql` creates it, `resources/app.yml` grants the app
`READ_VOLUME` and `WRITE_VOLUME` on it and passes the path as
`DBX_APP_VOLUME`.

It exists because **a Databricks App's own disk is not storage.** The app runs
at most 24 hours and then stops, taking its container with it; a redeploy does
the same. A file written beside the code is downloadable until the next
restart, which is worse than not offering it at all.

```bash
databricks sql query --file uc_ddl/003_app_volume.sql
```

Leaving it out is a supported, degraded deploy: `/healthz` reports `volume`
degraded and anything that would write a file is unavailable. Nothing on the
run path touches it — the job writes telemetry to Delta and results to their
own tables.

## Deploy

```bash
cd app/client && pnpm install && pnpm build && cd ../..   # only if the SPA changed
databricks bundle validate                           # schema and references
databricks bundle deploy -t dev
```

The build step is only needed when `app/client/` has changed since `app/dist/`
was last committed, because `app/dist/` is in git — which is also what makes
`databricks bundle deploy` work unchanged from inside a Databricks Git folder,
where there is no Node to run it.

**`pnpm build` runs `tsc -b` first**, so a type error fails the build rather
than shipping a stale bundle. Commit the result: a rebuilt `app/dist/` that is
not committed deploys the previous UI without saying so.

Skip the build when the SPA has not changed and nothing is lost. Skip it when
it *has* changed and the deploy succeeds, the API works, and every page serves
the old bundle — or, on a checkout that never built one, answers 503 with the
message in `app/server/spa.py::NO_BUNDLE`.

## Local development

Both halves run together, and it is the same code that deploys:

```bash
uv run python scripts/dev_stack.py     # FastAPI + a real job runner + Postgres
cd app/client && pnpm dev              # Vite, proxying /api, /ws, /healthz
```

`app/client/vite.config.ts` proxies to `DBX_DEV_API` (default
`http://127.0.0.1:8000`, matching `dev_stack.py::DEFAULT_APP_PORT`), so the
browser talks to the real FastAPI app — real SSE, real WebSocket ingress, real
`Last-Event-ID` resume. See `scripts/dev_stack.py`'s docstring for exactly
which parts are the shipped code and which are substituted.

To exercise what actually deploys instead — FastAPI serving the built bundle,
one process, no Vite — build first and open the app's own port:

```bash
cd app/client && pnpm build && cd ../..
uv run python scripts/dev_stack.py
```

## Lakebase

`run_status` lives in Postgres, not Delta — one row per run, point-looked-up,
and counted against the 5-concurrent-task ceiling. Postgres gives it a primary
key on `run_id` and a transaction around the count-and-claim, so that ceiling
is enforced rather than observed (`app/server/store.py`, `pg_advisory_xact_lock`).

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
which store is live and `app/server/services.py` logs it at startup, which is what
keeps that from being silent.

Apply `lakebase_ddl/001_run_status.sql` before the first run. The app applies
it at startup too, but a deploy that cannot reach the instance reports
`degraded: lakebase` rather than failing, so do not rely on that to tell you
the schema is there.

**The credential is the one piece unverified against a real workspace.**
Lakebase authenticates with a short-lived Databricks OAuth token — which is
why `app/server/store.py` opens a connection per operation instead of pooling, making
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

**The frontend is the exception to "the sync mirrors the repo".**
`databricks.yml` excludes `app/client/**` outright — the client source is
useless in a workspace with no Node runtime, and `node_modules` alone would
dwarf the rest of the sync. What deploys is `app/dist/`, which is not in that
directory: `app/client/vite.config.ts` writes `../dist`, so the bundle lands
at the app root, where `server/config.py::frontend_dist` looks for it by
default and `resources/app.yml` names it explicitly as `DBX_FRONTEND_DIST`.

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
  429 naming the limit (`app/server/routes/runs.py`),
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
