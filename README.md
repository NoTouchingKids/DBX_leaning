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
model in `job/models/`). The first five model tracks were briefed from a file in
`.claude/agents/`; once `job/models/README.md` and `/new-model` existed the
later six needed no brief at all, which is why there are fewer briefs there
than models.

## What's here

```
CLAUDE.md              Project brief, auto-loaded every session
docs/                  Architecture rationale, platform constraints, envelope spec, parallel plan
.claude/agents/        One brief per parallel track
.claude/commands/      /orient, /spike-ws, /spike-sse, /new-model

shared/                The message envelope + protocol. Imported by app/ and job/,
                       never by job/models/. Build against this, don't fork it.
job/                   The harness: model loader, thread->loop crossing, WS client
                       with HTTP-push fallback, Delta writer, cancellation
app/                   FastAPI: SSE to browsers, WS ingress for jobs, cancel,
                       backfill, startup reconciliation, ServiceHub/DI
job/models/                Eleven model packages. See job/models/README.md for the
                       duck-typed contract a model has to satisfy.
uc_ddl/                Unity Catalog DDL (telemetry), idempotent, apply in order
lakebase_ddl/          Postgres DDL (run state) — applied at startup too
databricks.yml         Asset bundle: eleven jobs (one per model) and the app
resources/             One job file per model — the microservice boundary
deploy/                Generated per-model requirements + the deployment guide
entrypoints/           What a Databricks job actually runs
app/                   The deployed app: server/ (FastAPI), client/ (React
                       source), dist/ (built SPA), shared/ (a tracked copy)
tests/                 ~790 tests, none needing a Databricks connection
scripts/               dev_stack.py — the whole platform locally, no workspace
                       dev_launcher.py — its stand-in for the Jobs API
                       check_gurobi_licence.py — the bundled-licence expiry
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
DBX_MODEL=job.models.scenario DBX_WRITER=jsonl DBX_ALLOW_LOCAL_WRITER=1 \
  uv run python -m job.main

uv run uvicorn server.main:app --reload    # the observer, on :8000
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

### The whole loop, with no workspace

The commands above run one piece at a time. This runs all of them together —
trigger a model from the browser and watch its telemetry arrive:

```bash
uv run python scripts/dev_stack.py      # app + job launcher + registry
cd frontend && pnpm dev                 # in a second terminal
```

Then click Run on any of the eleven models. That goes through `POST /api/runs`,
which launches the real `job/` harness in its own OS process, which attaches
over the real WebSocket ingress and streams real envelope messages back over
the real SSE endpoint. It is the shipped code, not a mock server and not
recorded fixtures.

Useful flags: `--models scenario,mcmc` to narrow what is triggerable,
`--max-concurrent-runs 1` to see the 429 without starting five runs,
`--reload` for uvicorn autoreload on `app/`, `--reset` to wipe the local
registry and telemetry. Kill the app process while a run is going and the
stack restarts it — the job keeps running and reattaches, which is the
autonomy property the transport design rests on, watchable in one terminal.

**What is real, and what is not.** A dev loop that quietly diverges from
production is how "works on my machine" gets built, so:

| | Local | Deployed |
|---|---|---|
| `app/`, `job/`, `shared/`, `job/models/` | the same code | the same code |
| Live path | real WS ingress, real HTTP-push fallback, real SSE | same |
| Run registry | embedded Postgres (`pgserver`) via `PostgresRunStore` | Lakebase, same class |
| Concurrency ceiling | the app's check is real; nothing enforces it behind that | the account's own 5-task limit too |
| **Trigger** | `scripts/dev_launcher.py` answers `run-now`/`runs/get` and spawns a subprocess; `DATABRICKS_HOST` points at it | the Jobs API |
| **Durable writes** | local JSONL under the state dir (`DBX_WRITER=jsonl`) | Delta in Unity Catalog via Spark |
| **Model environments** | one venv with everything | one serverless environment per model |
| **Warehouse reads** | none — backfill and `/results` answer 503, startup reconciliation is skipped | the SQL warehouse |
| Startup latency | milliseconds | tens of seconds for a serverless task |

`GET /healthz` reports `degraded` locally and names the reason; that is
expected, not a fault. Nothing is written inside the repository — state lives
under `~/.cache/dbx-leaning/dev-stack` (`--state-dir` to move it), including
job logs, so a failed run's traceback is a file away.

Each substitution is spelled out again in the docstrings of
`scripts/dev_stack.py` and `scripts/dev_launcher.py`, next to the code that
does it.

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
| `GET /api/models` | What can be triggered — derived from `DBX_JOB_IDS`, not by importing `job/models/` |
| `WS /ws/job/{id}` | The job's ingress, and the only inbound path to a running job |
| `POST /api/runs/{id}/push` | One-way HTTP fallback ingress |
| `GET /api/schema` | The wire protocol as JSON Schema — generate the client's types from this |
| `GET /api/whoami`, `GET /healthz` | Cosmetic identity; health with per-service degradation |

Triggering needs `DBX_JOB_IDS` (a JSON map of model name to Databricks job
id), `DATABRICKS_HOST`, and — to be observed rather than merely run —
`DBX_APP_PUBLIC_URL` so the job knows where to attach.

## State of play

`shared/`, `job/`, `app/` and all eleven models are built and tested, and
**WebSocket and SSE are both confirmed working through the Databricks Apps
ingress** — the question that stayed open across all three builds of this
platform (`docs/spike-results.md`). The transport in `docs/architecture.md` is
the one being built, not a hopeful guess.

Deployment exists as an Asset Bundle — eleven jobs, one per model, each with
its own serverless environment and dependency list exported from `uv.lock`.
See `deploy/README.md`.

What is **not** done: `databricks bundle deploy` has never actually been run
against a workspace from here. The bundle validates against the CLI's schema
and every contract it shares with the application code is tested, but
validation is not deployment. There is also no CI — nothing runs the suite on
a push.
