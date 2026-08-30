# v4 — the rewrite plan

Written 2026-08-30, from an audit of the deployed v3. This is the decision
record for the fourth build of this platform, and the argument for why it is
a *scoped* rewrite rather than a fourth from-scratch attempt.

**v3 stays.** It is deployed and it works. v4 is built on a branch beside it,
so there is always a running system to compare against and steal from. That
is a deliberate break from the v1→v2→v3 pattern, where each build began by
throwing the last one away and re-learning what it knew.

## The correction that comes first

**Every other doc in this repo says `databricks bundle deploy` has never been
run. That is false.** It was deployed around 2026-08-25..27. The git history
is unambiguous — these are commits nobody can write without a real workspace:

```
Find the repo root without __file__, which serverless does not set
Run the harness where a loop already exists, as serverless does
Get a Spark session even from a thread that did not create one
Wait for a starting warehouse instead of cancelling our own statement
Present a Databricks identity on the job's ingress, not just the app's secret
Discover the job map from the workspace when the environment omits it
Grant the app permission to run the jobs it knows about
```

This matters more than a stale line. Several documents reason *from* "not yet
deployed" as a premise — `CLAUDE.md` ("How to work in this repo"),
`docs/parallelization-plan.md`, `app/client/README.md`. Their conclusions are
unanchored until each is re-read against a deployed system.

**Rule for v4: no document may claim a deployment status.** It goes stale
faster than anything else in the repo, and it is the one fact every session
reads first. Status belongs in git, in the bundle, and in the workspace.

## The measurements this plan is built on

Taken 2026-08-30 against `dev`. `uv sync --all-extras && uv run pytest` →
**952 passed, 1 skipped in 45s**.

| Layer | Lines | Verdict |
|---|---|---|
| `shared/` — envelope + protocol | 900 | Keep. Lean and correct |
| `job/` harness (excl. models) | ~2,800 | Rewrite transport + durability; keep the model contract |
| `app/server/` | 3,561 | Rewrite ~half; delete the warehouse read path |
| `job/models/` — all eleven | 7,166 | Keep 1, archive 10 |
| **`app/client/src/components/models/`** | **20,117** | **Delete** |

The backend spine is **6,871 lines**. This was never a large system. The
per-model React views cost **2.8× the eleven models they visualise**, and 55%
of the frontend.

`docs/message-envelope-spec.md` states the thesis those views abandoned:

> The generic fields (`percent_complete`, `primary_metric`) let *any* model
> render a minimally useful progress view with **zero model-specific frontend
> code**.

That was right. v4 holds it as a hard rule: **a model earns a bespoke view
only after the generic view demonstrably fails it**, and never before the
model is deployed and streaming.

## What carries forward

Ported, not redesigned. Each survived eleven models and a real deployment
without changing shape.

- **`shared/envelope.py`** (290 lines). `seq`, `row_count`, `chunk_index` and
  `final` each exist because something broke without them; `row_count` is the
  only thing distinguishing "succeeded, wrote nothing" from "succeeded, wrote
  8,760 rows". Do not re-derive this. Port it and put a new transport under it.
- **SSE app→browser, unchanged.** It survives the ingress, `EventSource` gives
  reconnect and `Last-Event-ID` resume for free, and nothing about it is
  implicated in what went wrong. It is not in scope for this rewrite.
- **The duck-typed model contract.** Six models were added with zero harness
  changes, and `ortools_jobshop` surfaced a real cancellation gap that was
  fixed *inside the model's own callbacks* rather than with a new harness
  hook. That is the contract working.
- **The autonomy invariant.** The job runs; the app watches when it can. Apps
  die at 24h, jobs do not. Everything follows from this and it is not up for
  revision.
- **The app volume, which already exists.** `main.dbx_leaning.app_store` is
  created, granted, mounted at `/Volumes/main/dbx_leaning/app_store`, and
  already a `Path` in `app/server/services.py`. It keeps its current job —
  the app's own files: exports, uploads, cached artefacts. Run telemetry does
  **not** go here; it gets a second volume of its own (below). The pattern is
  proven and deployed, which is what carries forward.
- **The deployment lessons below.**

### Deployment lessons that must not regress

Extracted from the Aug 25–27 shakeout. A v4 that rediscovers these has wasted
the only thing v3 bought that v1 and v2 did not.

| Lesson | Where it bit |
|---|---|
| Serverless does not set `__file__` | Repo-root discovery |
| A `spark_python_task` runs inside an ipykernel that **already has a running event loop** — `asyncio.run` refuses | `job/main.py::_run` |
| The SQL warehouse is usually **asleep**; a query after a quiet period waits for it to start, and a naive timeout cancels your own statement | `app/server/sql.py` |
| A job needs **two** credentials on different headers — an OAuth identity for the Apps proxy, a shared secret for the app | `job/auth.py` |
| The job→model id map may be absent from the environment and must be discoverable from the workspace | `app/server/discovery.py` |
| Write against the **table's** schema, never one inferred from the batch | `job/delta.py` |
| A workspace secret scope is not a Unity Catalog object | `deploy/README.md` |

Note the second row. It is about to stop being a problem — see below.

## What gets deleted

- **The ten non-heartbeat models**, archived on `dev` and out of v4's tree
  until the platform is proven without them. Eleven models is why the platform
  cannot be experimented on: every transport change has 22 downstream
  consumers, and each new model costs a job file, a requirements export, a DDL
  block, a registry entry and ~1,800 lines of React.
- **All per-model frontend views** — 20,117 lines.
- **`WarehouseRunStore`** and the `RunStore` Protocol. It exists only so a
  deploy is never blocked on provisioning. Lakebase is provisioned.
- **`app/server/sql.py` + `repository.py`** (672 lines). With run state in
  Postgres, live backfill served by the job (below), and history arriving via
  the ingestion job, nothing on the app's live path needs the warehouse.
- **`job/delta.py`** (340 lines) and `DeltaRsWriter` with it.
- **`job/buffer.py` + `job/sink.py`** (225 lines) — the in-memory batching
  layer. v4 writes through to a file instead.
- **The triple copy of `shared/`.** `shared/`, `app/shared/`, `job/shared/`,
  plus a sync script and a drift test to make the duplication safe. v4 either
  packages it as a wheel or accepts one copy — not three.

## The v4 architecture

### Transport: one RPC-shaped channel over WS

v3 carries one envelope over **four paths with four different failure
semantics** — WS, HTTP push, Delta, SSE. That, not `protocol.py` (114 lines),
is the actual over-build.

v4: **an RPC-style interface over the WebSocket** — request/response with
correlation ids, methods dispatched from one table instead of a `ControlKind`
if-chain, one error shape. JSON-RPC 2.0 is the obvious wire format because it
is trivial and already understood; **the point is the RPC semantics, not the
specific framing.** gRPC is not a candidate — it would need HTTP/2 with
trailers through the Apps ingress, which is not what the spikes cleared, and
it buys nothing the design needs.

Three things this buys that v3 cannot do:

1. **Cancel gets an acknowledgement.** Today `_on_control` sets a token and
   replies nothing, so the app cannot distinguish "cancel delivered" from
   "cancel lost".
2. **Backfill becomes a method call** — see below. This is the strongest
   argument for the whole design.
3. **The job can ask the app things**, not only tell it.

### The job side is threaded, not asyncio

This is a significant simplification and it removes an entire class of bug.

`asyncio` currently spans **8 files in `job/` with 122 references**, and the
single ugliest workaround in the codebase exists purely to serve it:
`job/main.py::_run` detects that a serverless `spark_python_task` is already
inside an ipykernel event loop, and hands the run to a `ThreadPoolExecutor`
because `asyncio.run` refuses to nest. There is a 20-line comment explaining
it and a rejected `nest_asyncio` alternative.

**A threaded job deletes that problem rather than working around it.** The
shape:

- One thread owns the WebSocket. `websockets.sync.client.connect` — verified
  present in the pinned `websockets==17.0.1`.
- The model runs on the main thread, blocking, which is what a solver
  actually is. No `asyncio.to_thread` to keep the loop breathing.
- They meet over a `queue.Queue`. Cancellation stays a `threading.Event`,
  which it already is.

Gone with it: the loop exception handler that suppresses transport errors, the
task/pump/flusher choreography in `runner.py`, and the ipykernel dance. The
job becomes a program that does one thing on one thread and talks over a
socket on another.

### Durability: write through to a job-only volume

The job **stops buffering telemetry in memory** and instead writes each record
through to a file on a Unity Catalog volume — **a new one, separate from the
app's**:

```
/Volumes/main/dbx_leaning/log/runs/<run_id>/…
```

A **separate scheduled or streaming ingestion job** reads those files and
loads them into SQL. That is a different job on a different schedule; it is
not on the live path.

#### Two volumes, and the grant matrix is the architecture

| Volume | Purpose | App | Job | Ingestion |
|---|---|---|---|---|
| `main.dbx_leaning.app_store` | The app's own files — exports, uploads, cached artefacts | **READ + WRITE** | — | — |
| `main.dbx_leaning.log` | Run telemetry, written as it is produced | **none** | **READ + WRITE** | **READ** |

**The app has no grant on the log volume, and that is the point.** "The app
never reads run telemetry from files" stops being a design principle someone
has to remember and becomes something Unity Catalog refuses. A future session
cannot take the shortcut, because the shortcut returns a permission error.
This is the strongest form of the separation and it costs one `CREATE VOLUME`.

Consequences worth stating explicitly:

- The app should not even be **configured** with the log volume's path. No
  path, no grant, no temptation. `DBX_APP_VOLUME` keeps pointing at
  `app_store` and gains no sibling on the app side.
- The job needs `READ` as well as `WRITE`, because **replay reads back what it
  wrote** (below).
- The ingestion job is the only other principal with access, and only `READ`.

> **Naming.** `log` is narrower than what the volume holds — the envelope
> carries `progress`, `status` and `result` as well as `log`. `run_log` or
> `telemetry` would age better. Worth deciding now: renaming a volume later
> means touching grants, paths, the bundle and every written path prefix.

Consequences, all of them good:

- **Spark leaves the telemetry write path.** `job/delta.py` is 340 lines,
  most of it session archaeology — three acquisition strategies, a Spark
  Connect branch, and a thread-affinity bug fixed on Aug 27. A file write
  needs none of it.
- **The in-memory buffer goes.** `buffer.py` + `sink.py` (225 lines) collapse
  into "write a line, flush periodically". Durability stops depending on a
  flush policy holding data hostage.
- **Concurrency stops being a question.** Each run owns its own directory, so
  there is nothing to conflict — simpler than Delta's optimistic concurrency
  and its S3 locking caveat.
- **The 5-task ceiling is the only remaining shared resource.**

Retained: **a run must never report `SUCCEEDED` over a failed durable write.**
The check gets easier, not harder — it becomes "did the writes land", not
"did the flush of a buffer land".

> **⚠️ The one probe this plan actually needs.** Can a serverless job **append
> incrementally** to a file under `/Volumes/...`, holding a handle open across
> a long run? Volume FUSE supports sequential writes to new files, but "keep a
> handle open for an hour and flush repeatedly" is a stronger claim and is not
> documented as supported.
>
> **Fallback, which may simply be the design:** roll part files —
> `part-00001.jsonl`, `part-00002.jsonl`, each written whole and closed. That
> is append-only at the directory level, needs no FUSE append semantics, and is
> exactly the layout Auto Loader wants to ingest anyway. Probing decides
> whether the simpler single-file version is available; the fallback is safe
> either way.
>
> The probe runs against the new `log` volume with the job's own principal,
> so it exercises the grant matrix at the same time.

### Backfill: the job replays from its own log

When a client reconnects mid-run and finds a gap, **the app asks the job to
resend it** — `replay(from_seq, to_seq)` — and the job re-reads its own log
file and sends those records over the WebSocket.

This is the design's keystone, and it is why RPC-over-WS is worth doing:

- The app **cannot read the log volume** — it holds no grant on it — so this
  is not one option among several. It is the only live backfill path there is.
- No Files API dependency, no second credential path, no parsing the durable
  format in two places.
- The job is the authority on its own run while it is alive, and it already
  has the data on disk.
- Backfill stops being a warehouse query, which is the cost mistake this
  platform was built to avoid.

> **The consequence to accept deliberately.** For a **finished** run the job is
> gone, and the app still cannot read the log volume — so history has exactly
> one path: **SQL, after the ingestion job has run.** There is no fallback. If
> ingestion is broken or lagging, a completed run's telemetry is durable and
> correct on the volume and *invisible to the app*.
>
> In v3 the app could always fall back to reading Delta directly. That
> fallback is gone by design, and it is a fair trade — it is what makes the
> separation real rather than advisory. But it moves the ingestion job from
> "nice to have later" to **"required before the app can display any finished
> run"**. Slices 0–3 are unaffected: heartbeat, cancel and replay all concern a
> live run, where the job serves its own history.

### Run state: Lakebase Postgres

Unchanged, and already built. `run_status` is the one OLTP-shaped thing here —
a primary key on `run_id`, and a transaction around count-and-claim that makes
the 5-task ceiling real rather than advisory. Delta cannot do either.

### Auth: the SDK, for credentials only

`CLAUDE.md` currently states the `databricks-sdk` is "deliberately absent from
every dependency set." **That rule was half right, and the wrong half cost 343
lines** (`job/auth.py` 193, `app/server/oauth.py` 150) plus two of the Aug 27
fixes.

- **SDK for token acquisition and refresh.** Nobody should hand-roll OAuth
  token exchange, and this project's history is the evidence.
- **Plain `httpx` for the Jobs API.** The original reasoning holds: it is a few
  REST calls, and the SDK's weight would otherwise be paid by every model
  environment.

## Scope: heartbeat, and stop there

The ordering principle, and the one most different from v3: **prove comms with
no model in the picture.** This plan commits to Slices 0–3 only. Everything
after is a later decision made with a working platform in hand.

**Slice 0 — create the `log` volume, then one probe.** Apply the DDL and the
grants in the matrix above, then probe incremental volume append from a
serverless job with the job's own principal, with rolling part files as the
fallback. Half a day. Nothing else is unverified.

**Slice 1 — heartbeat, end to end.** A "model" that emits a tick a second and
nothing else, ~50 lines. Job (threaded) → RPC over WS → app → SSE → browser,
with records landing in the volume as they are produced. **Deployed, not
local.** When a tick appears in a browser from a deployed job and the files are
in the volume, the platform exists.

**Slice 2 — cancel, acknowledged.** Browser → app → job → ack → terminal
status. The first thing the RPC interface buys.

**Slice 3 — replay.** Cut the connection mid-run, reconnect, call
`replay(from_seq, to_seq)`, get the gap filled from the job's own log. Then
kill the app entirely and confirm the run completes and its files are intact.
That is the autonomy invariant tested rather than asserted.

**Slice 4 — the ingestion job (volume → SQL).** Promoted out of "later" by the
grant matrix: with the app locked out of the log volume, this is the *only*
way a finished run becomes visible. It is not on the live path and it is not
urgent for Slices 1–3, but the platform is not usable without it, so it should
not drift.

**Later, once the above is deployed and boring:** one real model on the generic
view, and only then the question of whether any model has *earned* a bespoke
view.

## The language decision, deferred on purpose

**Scope: the app/backend only. The job stays Python.** The models are Python
and always will be — Gurobi, OR-Tools, torch — and `job/models/_data` reads
Unity Catalog tables through the job's Spark session, so **pyspark stays in the
job for model data reads.** What changes is that it is no longer on the
telemetry write path, which is where all the pain was.

That leaves the app as the only open question: keep async Python (FastAPI, or
something else in that space), or take it somewhere compiled. Two things make
that decision cleaner than it was:

- The app's job is narrow — an RPC endpoint for jobs, SSE fan-out to browsers,
  Postgres for run state, a few REST calls to the Jobs API. That is a good fit
  for almost anything.
- It no longer needs Spark, Delta, or the SQL warehouse on the live path, so
  nothing about the platform forces Python on it.

Decide it after Slice 1 is deployed and the RPC surface is real, so the choice
is made against a known interface rather than an imagined one.

## What "done" means for v4

The bar v3 met, plus the three it did not:

1. A deployed run streams to a browser. *(v3: met)*
2. A run with no app listening is fully durable and readable afterwards.
   *(v3: designed for, never adversarially tested)*
3. **Cancel is acknowledged, and a gap can be replayed on demand.**
   *(v3: neither)*
4. **Adding a model costs no frontend code.** *(v3: failed, ~1,800 lines each)*
5. **The docs are true on the day they are read.** *(v3: failed, and it is why
   this document opens the way it does)*
