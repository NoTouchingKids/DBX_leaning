# DBX_leaning — Modelling Application Platform (v2 rewrite)

Read this file first, every session. Detail lives in `docs/`; this is the
map, not the territory. If something here conflicts with a file in `docs/`,
`docs/` wins — this file is a summary and can lag.

## What this is

A reusable internal platform on **Databricks Free Edition**: a React SPA +
async FastAPI app triggers and observes long-running analytical models —
eleven of them today: two Gurobi MILPs (scheduling, routing), an OR-Tools
CP-SAT job shop, scenario modelling, ML forecasting, MCMC, a conjugate
Bayesian A/B comparison, a small torch classifier, a simulated-annealing
knapsack, a bank of per-group curve fits over panel data, and a heartbeat
that computes nothing — that run as independent Databricks Jobs. Live progress, logs and results stream back to
the browser. Durable state lands in Unity Catalog via Delta.

This is a **rewrite**, not the first attempt. Two earlier builds exist in
this project's history (a Flask+Streamlit polling POC, then a FastAPI+
WebSocket app that was feature-complete but never deployed). Both taught real
lessons — see `docs/architecture.md` for what carried forward and why.

**v1 proved it was possible. This one has to actually work, and be
cost-effective.** Read that as: prefer the boring thing that runs over the
elegant thing that's still theoretical, and remember that on Databricks,
*uptime* costs money, not request count.

## Non-negotiable constraints (Databricks Free Edition)

These are verified, not assumed — see `docs/free-edition-constraints.md` for
sources. Design against them; do not build past them speculatively.

- **Max 5 concurrent job tasks per account**, across *all* models combined.
- **Apps run up to 24h** after start/update/redeploy, then stop. In practice
  this app runs ~8 business hours/day. Jobs are independent of the app and
  keep running (or start, or finish) while the app is down.
- **One SQL warehouse, 2X-Small only.** Warehouse cost is driven by
  **uptime**, not statement count (auto-stop min 5 min UI / 1 min API,
  default 10). This is why writes go through Delta, not the warehouse.
- **Outbound internet restricted to trusted domains.** This is why Gurobi
  uses the bundled restricted licence, not WLS (WLS needs to reach
  `token.gurobi.com`). It is also why a model **cannot fetch data over the
  internet at run time**: the `samples`-only restriction was lifted on
  2026-08-24 and external data is welcome, but it has to be landed in Unity
  Catalog first — a volume, Marketplace, or Delta Sharing. See
  `docs/free-edition-constraints.md`, "Getting data in from outside".
- Lakebase (managed Postgres) **is** available, and is used: it holds
  `run_status` and `run_status_history` (see Conventions), and it is also the
  fallback fan-out mechanism if this ever needs more than one app worker.

## Transport architecture (settled — do not redesign without reading `docs/architecture.md` first)

```
Live path    (job → app):      WebSocket, the only one, both directions
Live path    (app → client):   SSE, one-way
Durable path (job → UC):       Delta writer, ALWAYS, in parallel with the live path
Status path  (job → Lakebase): run_status + history, pg8000 straight to Postgres
```

- **Delta is the floor, not a fallback tier.** It runs regardless of whether
  the socket is up. A run observed live must still be fully persisted.
- **The job is autonomous; the app is an optional observer.** Jobs run on
  event/schedule/manual trigger, independent of whether the app is up. If the
  app is up and a `run_id` is live, the job attaches. If not, the run proceeds
  unobserved and stays fully durable — the app backfills from Delta later.
- **One live channel, not two.** The job's HTTP push fallback is gone
  (`job/bus.py` replaced `job/channels.py` + `job/relay.py`): the ingress
  probe settled that the socket survives, and a second one-way path that could
  carry neither a cancel nor a backfill was a second thing to keep in step for
  nothing. The app's `/api/runs/{run_id}/push` ingest endpoint is still there;
  the job no longer uses it.
- **The socket carries inbound: cancel, and backfill.** Cancel from the client
  always goes to the **app**, never a warehouse poll — a status poll would keep
  the warehouse awake for the run's duration, which is the exact cost mistake
  the first build made. `BACKFILL` is the same mistake refused from the other
  end: the job answers it from its own replay ring (`job/record.py`), so a
  browser tab waking up cannot wake the warehouse. Both ends are built:
  `GET /api/runs/{run_id}/messages` asks the job whenever there is a live
  socket and the gap sits inside the ring, and only falls through to the
  warehouse when the job cannot answer in full. The response says which
  answered, in `source`.
- **The boundary between the two backfills travels on the wire.** The job
  states `replay_from_seq` (the oldest seq it can still replay) and
  `flushed_through_seq` (how far Delta has caught up) on `hello` and on every
  backfill reply. Above the ring's floor, ask the job; below it, it is a full
  backfill and goes to SQL. Neither side keeps a tuned threshold of its own.
- **A live drop is recoverable now, so the drop policy got simpler.** The bus
  queue drops the oldest when full, whatever type it is. The old tiered
  eviction — logs before progress before status — existed because a dropped
  message was gone; it is not gone, it is in the durable buffer and in the
  replay ring. Teardown **drains before it closes** and pushes status and
  result to the front of what is left: the other order lost a fast run's
  entire live stream, terminal status included.
- **Workers = 1 for now.** The app's fan-out to browsers lives behind a
  `Broadcaster` interface so swapping in Lakebase `LISTEN`/`NOTIFY` later
  doesn't touch call sites.

## The message envelope

One schema, one `type` discriminator, for everything: `log`, `progress`,
`status`, `result`. Same shape whether it travels over the WebSocket, comes
back in a backfill reply, or is read back from Delta. Full spec:
`docs/message-envelope-spec.md`.

- `seq` is a single monotonic counter per run, **assigned by the job**, not
  by a UC identity column — it's the one thing that lets a client dedupe
  live vs backfilled records against each other.
- **Logs are progress/debugging, best-effort.** Drop under pressure on the
  live path; the durable path never drops.
- **Results are not best-effort.** Write results whenever the code reaches
  that point in execution, regardless of terminal status — a cancelled run
  keeps its incumbent. But a run must not report `SUCCEEDED` if its result
  write itself failed; the `result` message's `row_count` is what makes "zero
  results" distinguishable from "didn't get that far."
- **Packing:** msgpack job→app and in the Delta buffer; JSON on the SSE
  stream to the browser (native, readable in devtools, already compressed by
  the transport). Validation is Pydantic, kept **outside** the wire format —
  the envelope is a shape, not a serialiser.

## Conventions

- **uv, and the lockfile is the source of truth.** `uv sync`, `uv run pytest`,
  `uv add` — never bare `pip install` into the venv, which would put something
  in the environment that `uv.lock` does not describe. Databricks prefers uv,
  and the job needs an exact dependency set it can reproduce. Commit the lock.
  For anything that must have a `requirements.txt`, `uv export` from the lock
  rather than re-resolving.
- **ruff for lint, ty for types — ty advisory, not a gate.** ty is pre-1.0;
  run it, fix what it finds, do not fail a build on a young checker's opinion.
  It is scoped to source, not tests (see the note in `pyproject.toml`).
- **Async-first FastAPI.** SQL via the Statement Execution REST API over
  `httpx` — not `databricks-sql-connector`, not Spark from the app, and not
  the `databricks-sdk` either: it is deliberately absent from every
  dependency set, because the two APIs this needs (Statement Execution and
  Jobs) are a few plain REST calls and the SDK's weight would be paid by
  every model environment.
- **Run state lives in Lakebase (Postgres); telemetry lives in Delta.** Two
  tables in Postgres. `run_status` is current state — one row per run, updated
  on every transition, point-looked-up, counted against the concurrency
  ceiling — and Delta is poor at all three, on top of costing warehouse
  *uptime* to read. `run_status_history` is the append-only trace of those
  transitions beside it, so "how long did this sit QUEUED" stops being a
  warehouse question. `run_status` stays one upserted row and does **not**
  become append-only: the primary key on `run_id` is what refuses a duplicate
  run, and the 5-task ceiling is a count-and-claim inside one transaction —
  those are the two things Postgres was chosen over Delta for, and append-only
  would cost both, turning an indexed count into a window function over
  latest-row-per-run. Everything append-only and high-volume stays in Delta —
  logs, progress, events, results. **Progress does not follow status across**:
  10–500 rows a run with a fat `payload_json`, read by analytical scan and
  never point-looked-up. `run_events` stays too — it is the `status` quarter
  of the backfill stream and the only status record that does not ride a
  network call — and the planned end-of-run status write to UC's `run_status`
  is cancelled. `docs/architecture.md` has the split in full. See
  `app/server/store.py`; the warehouse-backed store remains as the
  unconfigured default so a deploy is never blocked on provisioning.
- **Two writers on that row, one concern each.** The **app** claims the slot —
  the count-and-claim transaction, and the row's creation at trigger time. The
  **job** owns the status transitions — the upsert on `run_status` and the
  append to `run_status_history` — straight to Postgres (`job/lakebase.py`,
  `DBX_LAKEBASE_DSN`), so `run_status` stays current for a run no socket ever
  attached to instead of sitting at whatever the app last saw. The job's
  upsert carries an `updated_ts` guard, so a retry delivered out of order
  cannot move the row backwards; the app's `set_status` does not, which
  `docs/architecture.md` records. This went over the Database REST API first,
  to keep a Postgres driver out of ten model environments; that endpoint is a
  PostgREST base and will not take raw SQL, so a driver is the cost of having
  the path at all.
- **Two drivers, one dialect, and the second one is not a preference.** The app
  uses psycopg; the job uses **pg8000**. psycopg[binary] dlopens a bundled
  libpq and OpenSSL, and in the Databricks serverless kernel — gRPC and
  pyarrow's native TLS already loaded — that import calls `abort()`: `Fatal
  Python error: Aborted`, no traceback, no run, and nothing catchable, since
  psycopg's own C/binary/ctypes fallback chain is `except Exception`. pg8000
  has no native code at all, so there is nothing to dlopen and nothing to
  abort; it is also ~120 KB against ~5 MB, in ten environments. It is
  synchronous, absorbed behind a thread hop. What survives the split is the
  dialect: `%(name)s` is psycopg's native placeholder style and pg8000's
  `pyformat`, so both writers still speak one language to one database.
- **No ORM.** Plain parameterised SQL text, bound parameters always —
  untyped parameters get compared as strings server-side (`"2" > "12"`), a
  bug the first build hit twice.
- **No module-level globals holding live objects.** In the app: a
  `ServiceHub` built in `lifespan`, stored on `app.state`, reached via
  FastAPI `Depends` — so a route needing a degraded/missing service gets a
  clean 503, not an `AttributeError`. In the job: a small singleton created
  in `main()` is fine — one process, one run.
- **Gurobi: bundled restricted licence only for this build.** No WLS. Cap is
  2000 variables / 2000 constraints (200 quadratic) — size models to fit.
  The bundled licence has **a fixed expiry per gurobipy release**; whatever
  version is pinned, record its expiry next to the pin. A problem that will
  not fit has somewhere to go: `job/models/ortools_jobshop` is CP-SAT,
  Apache-2.0, with no licence file, no expiry and no size cap at all.
- **Delta writes go through Spark**, behind one `write_batch(table, rows)`
  interface, implementation chosen once at startup. **delta-rs is gone, not
  deferred** — the class, the enum member and the sentinel error are all out
  of `job/delta.py`. It takes a storage URI, not a UC name, and given a
  three-part name it writes to a local directory without erroring, so a run
  would report SUCCEEDED with its telemetry in a container about to
  disappear; keeping a class that could only ever raise was carrying the shape
  of that mistake around. Building it for real needs credential vending and a
  table location, which is new work rather than a switch to flip. The other
  implementation is `JsonlWriter`, for local development. Flush on **size ≥ 1 MB
  OR age ≥ 30s (configurable) OR end-of-run** — the age bound is what caps
  data loss on a crash; size alone is not a durability guarantee.
- **VARIANT is nice-to-have, not required.** Fall back to a JSON string
  column if the environment doesn't support it cleanly.
- **`heartbeat` is the diagnostic model, and the only one that computes
  nothing.** It emits logs, progress and phase changes on a wall clock for a
  duration the caller picks — ten minutes by default, capped at thirty — so the
  live path has a run that is *still alive* by the time a browser has attached.
  Every other model finishes while the serverless session is still starting, so
  watching the socket, a backfill or a cancel meant getting lucky with timing.
  It is exempt from two rules that hold everywhere else, both by name in a test
  rather than by silence: it declares **no results table** (nothing to write, so
  no Delta write and no Unity Catalog dependency to fail on — a diagnostic that
  can fail for unrelated reasons is worse than none), and it has **no per-model
  view** in the SPA (its whole purpose is to exercise the generic run page).
- **A model is a plain Python object, not a class implementing an ABC.**
  Duck-typed discovery (look for a known set of method/attribute names) over
  an inheritance hierarchy — see `docs/architecture.md` for why. A model
  emits envelope-shaped messages to a callback it's handed; it does not know
  about WebSockets, Delta, or FastAPI.

## Repo layout (target)

```
app/            THE DEPLOYED APP — everything it needs, nothing else. This
                whole folder is `source_code_path`; see the note below
  server/       FastAPI application (async, SSE, ServiceHub, whoami)
  client/       React SPA source. Never deployed — `vite.config.ts` writes
                `../dist` and the bundle excludes `app/client/**` wholesale
  dist/         The built SPA. COMMITTED, because a deploy driven from inside
                Databricks has no Node runtime and sees only tracked files.
                Rebuild and commit it when the client changes, or the deployed
                UI is silently stale
  shared/       A TRACKED COPY of shared/ — `scripts/sync_shared.py` makes it,
                `tests/deploy/test_shared_copy.py` fails when it drifts
  app.yaml      Command + env read by the RUNTIME. `resources/app.yml` says
                the same to the BUNDLE; only that one can interpolate job ids.
                A deploy that is not `bundle deploy` reads this and nothing
                else — without it, "No command to run"
  requirements.txt  App deps, where Databricks Apps looks for them
job/            THE JOB UNIT — the harness plus its payload, its own floor
  run_model.py  What a Databricks task runs (workspace-file sync, not a wheel)
  (harness)     WS bus, run record + replay ring, Delta writer, Lakebase
                status reporter, model loader
  models/           One package per model — eleven: gurobi_scheduling/,
                gurobi_routing/, ortools_jobshop/, scenario/, forecasting/,
                mcmc/, bayesian_ab/, neural_net/, annealing/, panel_fit/ and
                heartbeat/ (plus _data/, the shared samples-catalog loaders —
                ortools_jobshop and panel_fit bring their own). Registered
                in [tool.dbx-leaning.models] in pyproject.toml
  shared/       A GENERATED copy, gitignored — `scripts/sync_shared.py` makes
                it and the bundle's preinit hook runs that before every sync.
                LOAD-BEARING: job/*.py imports `.shared`, relative, so `job` is
                one complete package from anywhere its parent is on sys.path.
                A fresh checkout cannot import job until it has been made;
                conftest.py does it before tests
  requirements.txt  The harness's floor. Each task installs
                deploy/requirements/<model>.txt, which is this plus one extra
shared/         The message envelope + protocol helpers, imported by both
                app/ and job/ (and indirectly by job/models/ via the callback
                they're handed — models never import shared/ directly)
uc_ddl/         Unity Catalog DDL (telemetry + per-model results tables)
lakebase_ddl/   Postgres DDL (run_status + history), applied at app startup
schema/         Generated JSON Schema for the wire protocol
scripts/        Registry, requirements/schema export, licence + sample probes
resources/      One job definition per model, plus the app — see below
deploy/         Generated per-model requirements, and the deployment guide
tests/          Offline; nothing here needs a Databricks connection
docs/           Everything referenced from this file
.claude/        Agents and commands — see below
```

**Each deployable unit is a folder that carries everything it needs.**

`app/` takes the shape of the Databricks app template — `server/` for the
FastAPI code, `client/` for the React source, `requirements.txt` at the app
root — one level down so the repo can hold the jobs too. `resources/app.yml`
gives Databricks Apps that folder as its `source_code_path` and **nothing
outside it travels**, which is why `shared/` has a tracked copy at
`app/shared/`. `job/` mirrors the shape: the harness, `job/models/`, its own
`requirements.txt`, its own copy.

The copies are copies rather than symlinks because **the workspace export
rejects symlinks** — the same rule that keeps `.venv` and
`app/client/node_modules` out of the sync.

**Both copies are load-bearing, and only one is committed.**

- `app/shared/` is **tracked**, and has to be. An app can be deployed without
  this bundle at all — the Apps UI, `databricks apps deploy
  --source-code-path ...` — and those see only what is in git.
- `job/shared/` is **generated and gitignored**. A job is only ever deployed by
  `databricks bundle deploy`, and `databricks.yml`'s `experimental.scripts.preinit`
  runs `scripts/sync_shared.py` before the sync. One canonical `shared/` in the
  repo; a complete `job/` in the workspace.

`job/*.py` imports `.shared` — **relative, its own copy** — so `job` is one
importable package from anywhere its parent is on `sys.path`, which is what
`job/run_model.py` arranges. The consequence: **a fresh checkout cannot import
`job` until the copy has been made.** `conftest.py` at the repo root does it
before collection; anything else wants `uv run python scripts/sync_shared.py`.

A second consequence, and it is the one that bites: `job.shared.envelope` and
`shared.envelope` are byte-identical source but **distinct types**, so
`MessageType.LOG is MessageType.LOG` is False across them. The two never meet
in a real process — the job emits bytes and the app parses them — but a test
that drives job code must build envelopes with `job.shared`. Tests under
`tests/job/`, `tests/integration/` and `tests/models/` do.

The duplication is a known compromise, scoped to this stage — packaging
`shared` as a wheel retires it. `tests/deploy/test_shared_copy.py` is what
makes it safe in the meantime: it fails the moment a copy differs, the moment
`app/shared/` stops being tracked, the moment `job/shared/` starts being, and
if the preinit hook goes missing.

## Deployment shape: a model is a microservice

**One job per model**, each with its own serverless environment and its own
dependency list. The MCMC job does not carry gurobipy; a model that later
needs GPU compute changes `resources/model_<name>.job.yml` and nothing else.
The job files repeat a little YAML rather than sharing an anchor — that
duplication is what lets them diverge.

- **Code travels by workspace file sync**, not as a wheel, for now. Moving to
  a wheel changes the task definition and nothing else.
- **Dependencies are exported from `uv.lock`** by
  `scripts/export_requirements.py`, never re-resolved — what deploys is what
  the tests ran against, and `tests/deploy/` fails if that stops being true.
- **Job parameters are a contract with `app/server/routes/runs.py`.** Databricks
  rejects a `run-now` parameter a job has not declared, so both sides are
  pinned to `JOB_PARAMETER_NAMES` and tested against each other.

Full procedure: `deploy/README.md`.

## How to work in this repo

1. Run `/orient` at the start of any session before writing code. It reads
   this file and the docs, and states back what it understood before
   touching anything.
2. **The two ingress probes gated everything, and both have passed** —
   WebSocket and SSE each survive the Databricks Apps ingress, confirmed
   against a real workspace on 2026-08-23 (`docs/spike-results.md`). Their
   *timings* are still unmeasured; `/spike-ws` and `/spike-sse` are how to
   fill those in. Nothing is blocked on them.
3. `shared/` (the envelope) was built first and sequentially — it is the one
   contract every other track depends on — and is frozen. Everything else
   parallelises. See `docs/parallelization-plan.md` for the worktree-per-track
   plan and which agent (`.claude/agents/*.md`) owns which track. When a new
   fan-out comes up, freeze its shared contract before starting it; the
   frontend did this again for its per-model views.
4. Frontend was explicitly low-priority until `app/`, `job/` and one model
   worked end to end. That gate is met and the track has started — see
   `app/client/README.md`.
5. **Deployed, but not verified end to end.** `databricks bundle deploy` has
   been run against a workspace many times, so the bundle resolves and creates
   its resources. That is not the same as a deployed run being checked, and it
   does not cover the UC DDL, which `deploy/README.md` applies as a separate
   manual step. Still not done: `scripts/probe_sample_data.py` has never been
   run end to end on a workspace, and there is no CI. The job's Lakebase
   writer is no longer on that list — `tests/job/test_lakebase_status.py`
   runs `REPORT_SQL` against a real PostgreSQL 16 over the real DDL, including
   the case that matters most: a stale transition leaves current state alone
   and still appends to history. The job **does** now hold a Databricks
   credential on a real workspace — `job.auth: app credential from the job's
   runtime identity`, from a serverless task on 2026-08-29 — which was the one
   thing gating both paths at once, since Lakebase takes an OAuth token as its
   password (`enable_pg_native_login: false`) and the Apps proxy wants one for
   the WebSocket. Neither path has been watched all the way through with it
   yet. And nothing writes `run_status_history` with `recorded_by='app'`, so a
   run's history starts at the job's first report and never records `QUEUED`.
   Do not read "built and tested" as "deployed".
   What *is* confirmed against a real workspace: WebSocket and SSE both
   survive the Apps ingress, the job resolving its own credential, the
   `samples` catalog's table list, and column listings for seven of its tables
   (`docs/sample-data-inventory.md`).

## Docs index

- `docs/architecture.md` — why, condensed from the full design conversation
- `docs/free-edition-constraints.md` — verified platform facts + sources
- `docs/message-envelope-spec.md` — the wire contract, in full
- `docs/parallelization-plan.md` — worktree strategy, track ownership, merge order
- `docs/spike-results.md` — the ingress probes: what they settled, what they didn't
- `docs/sample-data-inventory.md` — what is really in the `samples` catalog
- `docs/ml-datasets.md` — what is worth training on, and the three egress-free
  routes data can arrive by
- `docs/model-expansion-and-packaging.md` — per-model wheels, and what a next
  model would be for. Carries a status note: several of its premises are
  superseded, and it is the one doc to read the header of before the body
