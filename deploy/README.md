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

Four things, in this order. Only the first two are required.

**1. The Unity Catalog side — tables and the volume.** Apply the DDL once,
in order; every statement is `IF NOT EXISTS`, so re-running is safe.

```bash
export DBX_WAREHOUSE_ID=7474655945367403          # matches databricks.yml's default
databricks sql query --warehouse-id "$DBX_WAREHOUSE_ID" --file uc_ddl/001_core_tables.sql
databricks sql query --warehouse-id "$DBX_WAREHOUSE_ID" --file uc_ddl/002_model_results.sql
databricks sql query --warehouse-id "$DBX_WAREHOUSE_ID" --file uc_ddl/003_app_volume.sql
```

`001` is the one that matters: it is where every run's telemetry lands, and
without it a run fails at the end, after doing all the work. `002` is per-model
results, `003` is the app's volume. See `uc_ddl/README.md` for what skipping
each one costs, and for the `ALTER TABLE` note — `CREATE TABLE IF NOT EXISTS`
does not add a column to a table that already exists.

**2. The ingress secret.** The token a job presents to the app. The app reads
it from a secret; the job never stores it at all — the app passes it per run
as a job parameter at trigger time.

```bash
databricks secrets create-scope dbx-leaning
databricks secrets put-secret dbx-leaning app-token --string-value "$(openssl rand -hex 32)"
```

**3. A warehouse id**, for the app's read path only (backfill and startup
reconciliation). Nothing writes through it — see `docs/architecture.md` on why
the write path bypasses it entirely. Free Edition allows one, 2X-Small; its id
is `databricks.yml`'s `warehouse_id` default and `app/app.yaml`'s
`DBX_WAREHOUSE_ID`, and `tests/deploy/test_app_yaml.py` keeps those two equal.

**4. Lakebase, optional but wanted** — the Postgres instance holding
`run_status`. See the Lakebase section below for what leaving it out costs.
It is a Databricks-side resource this bundle does not create:

```bash
databricks database create-database-instance dbx-leaning --capacity CU_1
# waits for AVAILABLE by default; note `read_write_dns` in the output
databricks database get-database-instance dbx-leaning -o json

psql "host=<read_write_dns> port=5432 dbname=databricks_postgres user=<you> sslmode=require" \
  -f lakebase_ddl/001_run_status.sql
```

`databricks database` is Public Preview and its flags may move; check
`databricks database create-database-instance --help` if the above is refused.

**Choosing the Postgres major version — use the workspace UI.**
`databricks database create-database-instance` gives you **16**, and there is
no way to change that from the CLI:

- there is no `--pg-version` flag, and
- passing `pg_version` in `--json` is ignored. Tried on 2026-08-25 with
  `"pg_version": "PG_VERSION_18"`; the instance came back
  `"pg_version": "PG_VERSION_16"` with no error. The field exists on the
  object because it is returned, not because create accepts it.

The workspace UI (**Compute → Database instances → Create**) offers the
version. Create it there if you want 18, then carry on with the DNS name and
the DDL below.

**And it cannot be changed afterwards.** There is no `pg_version` flag on
`update-database-instance` either. Moving an existing instance to another
major means deleting it and creating a new one — cheap while it has not
served a run, since `run_status` is the only table here and `ensure_schema()`
rebuilds it at startup. No telemetry lives in Postgres; that is all in Delta.
The DNS name changes, so redeploy with the new `--var="lakebase_host=..."`.

**Nothing here needs a particular version.** `lakebase_ddl/001_run_status.sql`
uses primary keys, `ON CONFLICT`, advisory locks and a partial index, none of
which changed between 16 and 18. Rather than assert what a deployment got,
the app reads it: `PostgresRunStore.ensure_schema()` runs `SHOW
server_version` on the connection it already has open, and `GET /healthz`
returns it:

```json
"store": { "kind": "postgres", "server_version": "16.10" }
```

`kind` is the other half of that — a deployment that thinks it is on Lakebase
while silently running on the warehouse store keeps the concurrency race and
the missing primary key, and this is what makes that visible.

The app applies that schema at startup too, but a deploy that cannot reach the
instance reports `degraded: lakebase` rather than failing — so do not use
startup as proof the schema is there.

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
│   ├── app.yaml         command + env, read by the RUNTIME — see below
│   └── requirements.txt where Databricks Apps looks for it
├── job/                 <- the job unit: harness + payload + its own floor
│   ├── models/          eleven model packages, plus _data/
│   ├── shared/          a tracked copy too — not load-bearing yet, see below
│   └── requirements.txt the harness's floor; each task installs this + 1 extra
├── shared/              canonical. job/, job/models/ and tests import this one.
├── entrypoints/         what a task actually runs
└── databricks.yml  resources/  uc_ddl/       the bundle and its DDL
```

Three consequences, all of which have bitten this repo:

**`app/shared/` is a copy, and copies drift.** `app/` deploys alone, so
everything `server/` imports has to be inside it — and `server/` imports
exactly one first-party package, `shared`. It cannot be a symlink (see below).
`job/` carries the same copy for symmetry.
So one directory is canonical and the rest are copies:

```bash
uv run python scripts/sync_shared.py           # refresh them
uv run python scripts/sync_shared.py --check    # are they current?
```

`job/shared/` is the same copy, and it is **not load-bearing today**: a job
task runs `entrypoints/run_model.py` out of the whole synced repo tree, so it
imports the canonical `shared/` and never reads `job/shared/`. It is there so
`job/` is already a complete unit the moment it is packaged as a wheel.

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

The export fails on the FIRST symlink it meets, so the count is beside the
point — `.venv/bin` is full of them and `app/client/node_modules` carries
bin shims (22 under bun; pnpm's symlink-per-package store had thousands).
Both are in `databricks.yml`'s `sync.exclude`, and
`tests/deploy/test_bundle.py` asserts it.

**A sync exclude does not clean up what an earlier deploy already uploaded.**
If a previous run put `.venv` in the workspace, delete it there once:

```bash
databricks workspace delete /Workspace/Users/<you>/DBX_leaning/.venv --recursive
```

## Two ways to deploy, two files

`app/app.yaml` and `resources/app.yml` declare the same command and env, and
which one is read depends on how the app is deployed:

| how | reads |
| --- | --- |
| `databricks bundle deploy` | `resources/app.yml` — can interpolate `${var.*}` and job ids |
| Apps UI, `databricks apps deploy --source-code-path ...` | `app/app.yaml`, out of the source folder |

Deploying the second way without an `app.yaml` fails before the process
starts:

```
No command to run and no Python file found.
Please add a 'command' field to your app.yml file.
```

`tests/deploy/test_app_yaml.py` compares the two files so they cannot drift
into behaving differently. Watch the spelling — it differs by file, and both
are correct in their own:

```
app/app.yaml       (the runtime reads it)  ->  valueFrom
resources/app.yml  (the bundle declares)   ->  value_from
```

**`DBX_JOB_IDS` is only in the bundle.** Job ids do not exist until the bundle
creates the jobs, so a hand deploy cannot have them: `/healthz` reports "no
DBX_JOB_IDS configured; no model can be triggered from this app", and
everything else — observing runs someone else triggered, streaming, history,
results — works normally. Use `databricks bundle deploy` to get triggering.

**Bind `$DATABRICKS_APP_PORT`, never a literal.** Apps assigns the port. Bind
anything else and the platform's health check never connects, so the
deployment is marked FAILED while the app process runs fine.

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
cd app/client && bun install && bun run build && cd ../..   # only if the SPA changed
databricks bundle validate                           # schema and references
databricks bundle deploy -t dev
```

The build step is only needed when `app/client/` has changed since `app/dist/`
was last committed, because `app/dist/` is in git — which is also what makes
`databricks bundle deploy` work unchanged from inside a Databricks Git folder,
where there is no Node to run it.

**`bun run build` runs `tsc -b` first**, so a type error fails the build rather
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
cd app/client && bun run dev              # Vite, proxying /api, /ws, /healthz
```

`app/client/vite.config.ts` proxies to `DBX_DEV_API` (default
`http://127.0.0.1:8000`, matching `dev_stack.py::DEFAULT_APP_PORT`), so the
browser talks to the real FastAPI app — real SSE, real WebSocket ingress, real
`Last-Event-ID` resume. See `scripts/dev_stack.py`'s docstring for exactly
which parts are the shipped code and which are substituted.

To exercise what actually deploys instead — FastAPI serving the built bundle,
one process, no Vite — build first and open the app's own port:

```bash
cd app/client && bun run build && cd ../..
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

### Running as a service principal you granted yourself

By default the app authenticates as the service principal Databricks Apps
creates for it, and **nothing needs configuring**. Using one you made and
granted explicitly is opt-in, in three steps that must happen in this order:

```bash
# 1. The secret must EXIST before the bundle may mention it.
databricks secrets put-secret dbx-leaning oauth-client-secret \
  --string-value "<the SP's OAuth secret>"

# 2. Uncomment BOTH blocks in resources/app.yml - the `oauth-client-secret`
#    resource, and the DBX_OAUTH_CLIENT_SECRET env that reads it.

# 3. Deploy, naming the principal.
databricks bundle deploy -t dev \
  --var="oauth_client_id=<the SP's application id>" \
  --var="lakebase_host=<read_write_dns>" \
  --var="lakebase_user=<the SP's application id>"
```

**Why it ships commented out rather than just empty.** A declared secret
resource is validated when the app is updated, so a bundle that mentions a key
the scope does not have fails the whole deploy before uploading anything:

```
Error: cannot update resources.apps.dbx_leaning:
Invalid secret resource oauth-client-secret: Secret with scope dbx-leaning
and key oauth-client-secret does not exist. (404 NOT_FOUND)
```

There is no fallback for a *declared* resource — declaring it makes it
required. Everything else optional in this platform degrades at run time and
reports itself on `/healthz`; this one cannot, so it is off until you opt in.
`tests/deploy/test_bundle.py` fails if it is ever declared by default.

Setting `oauth_client_id` without doing step 2 is inert but not silent:
`/healthz` reports `oauth` degraded, naming the id it was given, because
falling back to the app's own principal while a Lakebase role is granted to a
different one is a failure that otherwise looks like success.

**`lakebase_user` is the SAME application id, and this is the mistake to
avoid.** Lakebase takes an OAuth token as its password, and the Postgres role
Databricks provisions is named after the principal that token belongs to.
Connecting as one principal while presenting another's token fails as a plain
authentication error — indistinguishable from a wrong secret, so the hour goes
into the secret scope. `tests/deploy/test_bundle.py` refuses the mismatch in
the bundle, and `/healthz` reports `lakebase_identity` at run time if the two
env vars ever disagree.

**The id is a variable, the secret is a secret.** A bundle variable ends up in
the deployment state, which is readable by a different set of people than the
secret scope; `tests/deploy/test_bundle.py` fails if any variable's name looks
like a credential. Grant the principal `READ_VOLUME`/`WRITE_VOLUME` on the
volume, whatever your models need on the results tables, `CAN_MANAGE_RUN` on
the jobs, and a Postgres role on the Lakebase instance.

**One token, three uses.** `server/services.py::_token_source` builds a single
`OAuthTokenProvider` and hands it to the run store, the SQL client and the
Jobs API, so there is one exchange and one cache rather than three on
independent refresh schedules.

### The credential is a token, not a setting

An instance created with `enable_pg_native_login: false` — **the default** —
accepts only a short-lived Databricks OAuth token as its Postgres password.
There is no password to put in a secret, and none is set.

So the app fetches one, per connection:

```
DATABRICKS_CLIENT_ID / DATABRICKS_CLIENT_SECRET   injected by Databricks Apps
    -> POST {host}/oidc/v1/token, grant_type=client_credentials
    -> the token becomes the Postgres password for that connection
```

`server/oauth.py` caches it until shortly before it expires, so the common
case is a dict lookup and the uncommon one is a single round trip. This is
also the reason `server/store.py` opens a connection per operation rather than
pooling.

**The obvious alternative is a bug, and this app had it.** Reading the
credential once at startup and building a connection string from it works —
for about an hour. An App runs for up to 24, so a deployment sees every
Postgres operation succeed through the morning and fail thereafter, with
nothing in the logs pointing back at startup, which is where the fault is.

Two things about this remain unverified against a real workspace: that the
Apps runtime injects those two variables under those names, and that
`scope=all-apis` is granted to an app's service principal. Both are
overridable — `DBX_OAUTH_CLIENT_ID` and `DBX_OAUTH_CLIENT_SECRET` take
precedence — and a failed exchange raises `TokenUnavailable` naming the
endpoint and OAuth's own error code rather than sending an empty password to
Postgres, which would surface as a password mismatch and send you looking for
the wrong problem.

**A static password is still supported**, for the local dev stack (embedded
Postgres, no auth) and for an instance created with native login on: set
`DBX_LAKEBASE_PASSWORD` — as a **secret**, never a bundle variable, since a
variable ends up in the deployment state. The YAML is commented in
`resources/app.yml`, and `tests/deploy/test_bundle.py` fails if a credential
is ever added as a variable.

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
  "job_parameters": {"DBX_RUN_ID": "manual-1", "DBX_MODEL": "job.models.scenario"}
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
