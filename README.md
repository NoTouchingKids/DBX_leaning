# DBX_leaning

Databricks modelling application platform — v2 rewrite. See `CLAUDE.md` for
the full brief; this file is just "what do I do first."

## First run

```
claude
> /orient
```

That reads `CLAUDE.md` and `docs/`, and states back what it understood
before writing anything — check it agrees with you before continuing.

## Before any real building

Two platform questions gate everything else. Run them first:

```
> /spike-ws
> /spike-sse
```

Both are small, throwaway, and answer whether the transport this whole
design leans on actually works on Databricks Apps. Everything else in the
design has a documented fallback (see `docs/architecture.md`); these two
don't, so they go first.

## After the probes pass

`docs/parallelization-plan.md` has the worktree-per-track breakdown. Short
version: build `shared/` (the message envelope) once, sequentially, then
fan out — one Claude Code session per track (`app/`, `job/`, and one per
model in `models/`), each briefed from its file in `.claude/agents/`.

## What's here

```
CLAUDE.md              Project brief, auto-loaded every session
docs/                  Architecture rationale, platform constraints, envelope spec, parallel plan
.claude/agents/        One brief per parallel track
.claude/commands/      /orient, /spike-ws, /spike-sse, /new-model

shared/                The message envelope + protocol. Imported by app/ and job/,
                       never by models/. Build against this, don't fork it.
job/                   The harness: model loader, thread->loop crossing, WS client
                       with HTTP-push fallback, Delta writer, cancellation
app/                   FastAPI: SSE to browsers, WS ingress for jobs, cancel,
                       backfill, startup reconciliation, ServiceHub/DI
models/                Five model packages. See models/README.md for the
                       duck-typed contract a model has to satisfy.
uc_ddl/                Unity Catalog DDL (telemetry), idempotent, apply in order
lakebase_ddl/          Postgres DDL (run state) — applied at startup too
databricks.yml         Asset bundle: five jobs (one per model) and the app
resources/             One job file per model — the microservice boundary
deploy/                Generated per-model requirements + the deployment guide
entrypoints/           What a Databricks job actually runs
frontend/              Not started, on purpose — see frontend/README.md
tests/                 ~220 tests, none needing a Databricks connection
scripts/               check_gurobi_licence.py — the bundled-licence expiry
```

## Running it locally

[uv](https://docs.astral.sh/uv/) manages the environment, and `uv.lock` is
committed — everything below resolves to exactly the versions the tests ran
against.

```bash
uv sync --all-extras                    # or just what you need, see below

uv run pytest                           # everything, offline
uv run ruff check .                     # lint
uv run ty check                         # types (advisory — see below)

# a full run with no app listening — the normal unobserved case
DBX_MODEL=models.scenario DBX_WRITER=jsonl DBX_ALLOW_LOCAL_WRITER=1 \
  uv run python -m job.main

uv run uvicorn app.main:app --reload    # the observer, on :8000
```

Extras are separable, and the lockfile covers all of them from one
resolution — so a partial install is a subset of the tested world, never a
different resolution of it:

```bash
uv sync --extra job --extra gurobi      # the scheduling model, no sklearn/emcee
uv sync --extra app                     # just the observer
```

For anything that needs pip (a Databricks App's `requirements.txt`, say),
export from the lock rather than re-resolving:

```bash
uv export --no-dev --extra app --format requirements-txt -o requirements-app.txt
```

**Types are advisory.** `ty` is pre-1.0, so it runs on demand rather than
gating anything, and it is scoped to the source packages — the test fixtures
splat dicts into dataclasses and inject duck-typed stubs, which a static
checker objects to and which are not defects. Source sits at zero errors; the
one standing warning is `pyspark`, deliberately absent from the dependency set
because the Databricks runtime provides it.

## API surface

| Endpoint | What |
|---|---|
| `POST /api/runs` | Trigger a model. Launches the Databricks job and registers the run |
| `GET /api/runs` | Recent runs, each flagged with whether a job is live on it |
| `GET /api/runs/{id}` | One run's current state |
| `GET /api/runs/{id}/stream` | SSE. `id:` is the message `seq`, so `EventSource`'s own `Last-Event-ID` resume works unmodified |
| `GET /api/runs/{id}/messages` | Explicit backfill from Unity Catalog, client-triggered, paged by seq |
| `GET /api/runs/{id}/results` | The full result set a `result` message only previews — the table its `fetch_hint` points at, paged |
| `POST /api/runs/{id}/cancel` | Forwards over the job's WebSocket, or 409s naming the CLI escape hatch |
| `GET /api/models` | What can be triggered — derived from `DBX_JOB_IDS`, not by importing `models/` |
| `WS /ws/job/{id}` | The job's ingress, and the only inbound path to a running job |
| `POST /api/runs/{id}/push` | One-way HTTP fallback ingress |
| `GET /api/whoami`, `GET /healthz` | Cosmetic identity; health with per-service degradation |

Triggering needs `DBX_JOB_IDS` (a JSON map of model name to Databricks job
id), `DATABRICKS_HOST`, and — to be observed rather than merely run —
`DBX_APP_PUBLIC_URL` so the job knows where to attach.

## State of play

`shared/`, `job/`, `app/` and all five models are built and tested, and
**WebSocket and SSE are both confirmed working through the Databricks Apps
ingress** — the question that stayed open across all three builds of this
platform (`docs/spike-results.md`). The transport in `docs/architecture.md` is
the one being built, not a hopeful guess.

Deployment exists as an Asset Bundle — five jobs, one per model, each with
its own serverless environment and dependency list exported from `uv.lock`.
See `deploy/README.md`.

What is **not** done: `databricks bundle deploy` has never actually been run
against a workspace from here. The bundle validates against the CLI's schema
and every contract it shares with the application code is tested, but
validation is not deployment. There is also no CI — nothing runs the suite on
a push.
