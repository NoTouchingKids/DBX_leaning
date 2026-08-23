# DBX_leaning — Modelling Application Platform (v2 rewrite)

Read this file first, every session. Detail lives in `docs/`; this is the
map, not the territory. If something here conflicts with a file in `docs/`,
`docs/` wins — this file is a summary and can lag.

## What this is

A reusable internal platform on **Databricks Free Edition**: a React SPA +
async FastAPI app triggers and observes long-running analytical models
(Gurobi optimisation, scenario modelling, ML forecasting, MCMC, one more)
that run as independent Databricks Jobs. Live progress/logs/results stream
back to the browser. Durable state lands in Unity Catalog via Delta.

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
  `token.gurobi.com`).
- Lakebase (managed Postgres) **is** available — the fallback fan-out
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
  write itself failed; track `result_row_count` so "zero results" is
  distinguishable from "didn't get that far."
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
- **Async-first FastAPI.** SQL via the Databricks SDK / REST API, not
  `databricks-sql-connector`, not Spark from the app. `httpx` for non-blocking
  HTTP.
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
  version is pinned, record its expiry next to the pin.
- **Delta writes: delta-rs preferred, Spark fallback**, behind one
  `write_batch(table, rows)` interface, implementation chosen once at
  startup. Flush on **size ≥ 1 MB OR age ≥ 30s (configurable) OR
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
app/            FastAPI application (async, SSE, ServiceHub, whoami)
job/            Job harness (WS client, HTTP push, Delta writer, model loader)
models/         One package per model — gurobi_scheduling/, scenario/,
                forecasting/, mcmc/, streaming_results/
shared/         The message envelope + protocol helpers, imported by both
                app/ and job/ (and indirectly by models/ via the callback
                they're handed — models never import shared/ directly)
frontend/       React SPA (back burner until app/job/one model works)
uc_ddl/         Unity Catalog DDL
docs/           Everything referenced from this file
.claude/        Agents and commands — see below
```

## How to work in this repo

1. Run `/orient` at the start of any session before writing code. It reads
   this file and the docs, and states back what it understood before
   touching anything.
2. **Nothing below is buildable until the two ingress probes pass.**
   Run `/spike-ws` and `/spike-sse` first. Both are small and answer real
   platform questions — everything else has a documented fallback, these two
   don't.
3. After the probes: build `shared/` (the envelope) first, sequentially — it
   is the one contract every other track depends on. Then everything else
   parallelises. See `docs/parallelization-plan.md` for the worktree-per-track
   plan and which agent (`.claude/agents/*.md`) owns which track.
4. Frontend is explicitly low-priority until `app/`, `job/`, and one model
   work end to end.

## Docs index

- `docs/architecture.md` — why, condensed from the full design conversation
- `docs/free-edition-constraints.md` — verified platform facts + sources
- `docs/message-envelope-spec.md` — the wire contract, in full
- `docs/parallelization-plan.md` — worktree strategy, track ownership, merge order
