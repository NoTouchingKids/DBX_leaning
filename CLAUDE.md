# DBX_leaning — Modelling Application Platform (v2 rewrite)

Read this file first, every session. Detail lives in `docs/`; this is the
map, not the territory. If something here conflicts with a file in `docs/`,
`docs/` wins — this file is a summary and can lag.

## What this is

A reusable internal platform on **Databricks Free Edition**: a React SPA +
async FastAPI app triggers and observes long-running analytical models —
eleven of them today: two Gurobi MILPs (scheduling, routing), an OR-Tools
CP-SAT job shop, scenario modelling, ML forecasting, MCMC, a conjugate
Bayesian A/B comparison, a small torch classifier, a chunked rolling
backtest, a simulated-annealing knapsack and a bank of per-group curve fits
over panel data — that run as independent Databricks Jobs. Live progress,
logs and results stream back to the browser. Durable state lands in Unity
Catalog via Delta.

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
  `run_status` (see Conventions), and it is also the fallback fan-out
  mechanism if this ever needs more than one app worker.

## Transport architecture (settled — do not redesign without reading `docs/architecture.md` first)

```
Live path    (job → app):    WebSocket  →  HTTP push (fallback)
Live path    (app → client): SSE, one-way
Durable path (job → UC):     Delta writer, ALWAYS, in parallel with the live path
```

- **Delta is the floor, not a fallback tier.** It runs regardless of whether
  a live channel is up. A run observed live must still be fully persisted.
- **The job is autonomous; the app is an optional observer.** Jobs run on
  event/schedule/manual trigger, independent of whether the app is up. If the
  app is up and a `run_id` is live, the job attaches. If not, the run proceeds
  unobserved and stays fully durable — the app backfills from Delta later.
- **Only WS carries inbound** (cancel commands). HTTP push is one-way. Cancel
  from the client always goes to the **app**, never a warehouse poll — a
  status poll would keep the warehouse awake for the run's duration, which is
  the exact cost mistake the first build made.
- **Workers = 1 for now.** The relay lives behind a `Broadcaster` interface
  so swapping in Lakebase `LISTEN`/`NOTIFY` later doesn't touch call sites.

## The message envelope

One schema, one `type` discriminator, for everything: `log`, `progress`,
`status`, `result`. Same shape whether it travels over WS, HTTP push, or is
read back from Delta. Full spec: `docs/message-envelope-spec.md`.

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
- **Run state lives in Lakebase (Postgres); telemetry lives in Delta.**
  `run_status` is the one OLTP-shaped thing here — one row per run, updated on
  every transition, point-looked-up, counted against the concurrency ceiling.
  Delta is poor at all three and reading it costs warehouse *uptime*. Postgres
  also buys what Delta structurally cannot: a primary key on `run_id`, and a
  transaction around the count-and-claim so the 5-task ceiling is real rather
  than advisory. Everything append-only — logs, progress, events, results —
  stays in Delta. See `app/server/store.py`; the warehouse-backed store remains as
  the unconfigured default so a deploy is never blocked on provisioning.
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
  not fit has somewhere to go: `models/ortools_jobshop` (on `dev`) is CP-SAT,
  Apache-2.0, with no licence file, no expiry and no size cap at all.
- **Delta writes go through Spark**, behind one `write_batch(table, rows)`
  interface, implementation chosen once at startup. delta-rs remains the
  target but is **not implemented and must not be selected**: it takes a
  storage URI, not a UC name, and given a three-part name it writes to a
  local directory without erroring — a run would report SUCCEEDED with its
  telemetry in a container that is about to disappear. It raises
  `NotImplementedError` rather than doing that. Building it needs credential
  vending; see `job/delta.py`. Flush on **size ≥ 1 MB OR age ≥ 30s (configurable) OR
  end-of-run** — the age bound is what caps data loss on a crash; size alone
  is not a durability guarantee.
- **VARIANT is nice-to-have, not required.** Fall back to a JSON string
  column if the environment doesn't support it cleanly.
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
                Databricks has no Node runtime and sees only tracked files
  shared/       THE message envelope + RPC frames. The canonical copy, not a
                copy of one — it lives here because the app deploys alone and
                cannot reach outside this folder. The job gets the same module
                by installing the repo; see the note below
  app.yaml      Command + env read by the RUNTIME. `resources/app.yml` says
                the same to the BUNDLE. A deploy that is not `bundle deploy`
                reads this and nothing else — without it, "No command to run"
  requirements.txt  App deps, where Databricks Apps looks for them
job/            THE HARNESS. Loads a model, drives it, gets its messages onto
                the durable and live paths. Carries no model
  run_model.py  What a Databricks task runs — 41 lines, no path machinery
  local.py      `run_local(model, **config)` — the same harness, no Databricks
  (harness)     WS/RPC client, telemetry part-file writer, model loader, auth
  requirements.txt  The harness floor, and the whole environment. A model's
                libraries are the model's own business
models/         ONE INSTALLABLE PACKAGE PER MODEL, each its own distribution
                with its own dependency list and ONE entry point. Discovered
                by `importlib.metadata`, not by a registry — so a model in
                another repository works identically. See models/README.md.
                Only `heartbeat/` on this branch; the other eleven are on `dev`
uc_ddl/         Unity Catalog DDL (telemetry volume + results tables)
lakebase_ddl/   Postgres DDL (run_status), applied at app startup
schema/         Generated JSON Schema for the wire protocol
scripts/        Requirements/schema export, licence + sample probes
resources/      One job definition per model, plus the app — see below
deploy/         The deployment guide
tests/          Offline; nothing here needs a Databricks connection
  container/    EXCEPT these: real Docker images, one per deployable shape,
                each seeing only what its Databricks counterpart would. Opt-in
                (`DBX_CONTAINER_TESTS=1`), skipped otherwise. They exist because
                a subprocess inherits sys.path and a container does not — see
                tests/container/README.md
docs/           Everything referenced from this file
.claude/        Agents and commands — see below
```

**Each deployable unit installs packages; nothing is synced as loose files.**

That sentence replaces the v3 rule it reads like ("each unit is a folder that
carries everything it needs"), and the change is worth understanding because a
surprising amount of machinery existed only to serve the old one.

v3 synced `job/` into the workspace as plain files, which are on nobody's
`sys.path`. So `job/*.py` imported `.shared` — relative, from its own generated
copy — `run_model.py` searched four ways for a repo root, `scripts/sync_shared.py`
made two copies of `shared/`, `databricks.yml` ran it from a preinit hook, and
`tests/deploy/test_shared_copy.py` failed when the copies drifted. A fresh
checkout could not `import job` until the copy had been made. Worse, `job.shared.envelope`
and `shared.envelope` were byte-identical source but **distinct types**, so
`MessageType.LOG is MessageType.LOG` was False across them.

All of it is gone. The job environment installs `${workspace.file_path}` — this
repo, as a distribution — plus the model's own package, and Python finds an
installed package the ordinary way. There is no root to find, no copy to make,
no hook, no drift test, and no second `MessageType`.

**Where `shared/` lives, and why it looks wrong.** `resources/app.yml` hands
Databricks Apps `../app` as its `source_code_path` and **nothing outside that
folder travels** — an app can also be deployed with no bundle at all, from the
Apps UI or `databricks apps deploy --source-code-path ...`. So the envelope has
to be physically inside `app/` or the app cannot import what it parses. It is
therefore canonical at `app/shared/`, and `[tool.setuptools] package-dir` in
`pyproject.toml` maps it back out so the job gets the same module. One
directory in a slightly odd place, instead of two copies and a script.

A symlink would not do: **the workspace export rejects symlinks** and fails on
the first one it meets — the same rule that keeps `.venv` and
`app/client/node_modules` out of the sync.

`tests/deploy/test_app_is_self_contained.py` is what keeps this honest. Deleting
`app/shared/` as apparent duplication broke the deployed app and the suite
stayed green, because pytest has the repo root on its path and the workspace
does not. That test walks `server/`'s imports and fails if one resolves to
something outside `app/`.

`tests/container/` is the version of that check that cannot be fooled: it
builds the app from `app/` as the Docker build context and starts it, so the
repo is absent from the disk rather than merely unused. It also builds the
app WITHOUT `shared/` and asserts that one fails — a green test that cannot go
red would not have caught this either.

**A model depends on nothing here.** `models/heartbeat/pyproject.toml` has an
empty `dependencies` list, and that is the proof rather than an accident of a
trivial model: a model imports neither `job` nor `shared`, and reaches the
platform only through the `emit` callback it is handed. Discovery is by entry
point (`[project.entry-points."dbx_leaning.models"]`), so `DBX_MODEL` is a
NAME — `heartbeat` — not an import path, and a model that moves to its own
repository is found the same way.

One rule that does not follow from the above and still holds: a model must not
appear in `[project.dependencies]`. `[tool.uv.sources]` marks it a workspace
member, which **only uv reads** — and the job environment installs this repo
with pip, which would go looking on PyPI and fail the deploy.

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
5. **Not done:** `databricks bundle deploy` has never been run against a
   workspace, `scripts/probe_sample_data.py` has never been run end to end on
   one, and there is no CI. Do not read "built and tested" as "deployed".
   What *is* confirmed against a real workspace: WebSocket and SSE both
   survive the Apps ingress, the `samples` catalog's table list, and column
   listings for seven of its tables (`docs/sample-data-inventory.md`).

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
