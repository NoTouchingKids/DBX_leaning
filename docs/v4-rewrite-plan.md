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

- **`shared/envelope.py`** (290 lines), with **one deliberate change** (below).
  `seq`, `row_count`, `chunk_index` and `final` each exist because something
  broke without them; `row_count` is the only thing distinguishing "succeeded,
  wrote nothing" from "succeeded, wrote 8,760 rows". Do not re-derive this.
  Port it and put a new transport under it.
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
- **`job/delta.py` entirely** (340 lines). Not reduced — redundant. The
  harness writes no tables at all once the model owns its own results; see
  "Who writes what" below. `emitter.py::_absorb_result_rows` and the
  `results_table` / `preview_axes` plumbing go with it.
- **`job/buffer.py` + `job/sink.py`** (225 lines) — the in-memory batching
  layer. v4 writes through to a file instead.
- **The triple copy of `shared/`.** `shared/`, `app/shared/`, `job/shared/`,
  plus a sync script and a drift test to make the duplication safe. v4 either
  packages it as a wheel or accepts one copy — not three.

## The v4 architecture

### The direction every boundary below is drawn from

**Event-driven, near-real-time, microservice-shaped now; genuinely separate
services later.** This is the principle that explains the rest of this
document, and it should be the tie-breaker whenever a decision here looks
arbitrary.

Concretely, it means:

- **A job is a service, not a subroutine.** It runs on a schedule or an event.
  The app may *trigger* one, and that is the only thing the app does to a job
  that the job cannot do without it. Everything else — reading its inputs,
  computing, writing its results, recording its telemetry, reaching a terminal
  status — happens whether or not the app exists.
- **The app is a trigger and an observer.** Not an orchestrator, not a
  persistence layer, not the owner of anyone's data.
- **A model owns its own data lifecycle**, input and output both. The harness
  is a comms and telemetry layer around it.
- **The app runs ~8h/day and the job does not.** A scheduled run at 3am is the
  normal case, not the degraded one, and it is the case every design decision
  here is checked against.

The v3 invariant ("the job is autonomous, the app is an optional observer")
was the same idea stated defensively. v4 states it as the target shape: these
are services that happen to share a repo today, and the boundaries are drawn
so that they can stop sharing one without a redesign.

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

**What travels on it, unchanged from v3 and worth restating because it is easy
to misread:** the wire carries `log`, `progress`, `status`, and a `result`
*summary* — a bounded preview (≤1000 points, LTTB-downsampled, enough to draw
the chart), a `row_count`, and a `fetch_hint` pointer. **The wire contract
never carries a result set.** `emitter.py::_absorb_result_rows` strips `rows`
off the message before it reaches any channel.

Three things this buys that v3 cannot do:

1. **Cancel gets an acknowledgement.** Today `_on_control` sets a token and
   replies nothing, so the app cannot distinguish "cancel delivered" from
   "cancel lost".
2. **Backfill becomes a method call** — see below. This is the strongest
   argument for the whole design.
3. **The job can ask the app things**, not only tell it.

### The one envelope change: status is a string, and terminality is stated

`RunStatus` stops being a closed six-value enum and becomes **a plain string**.
That is what makes model-defined statuses possible at all — the open question
from the run-state section resolves here, by not needing a second field. It is
also what makes the contract portable: a closed enum has to be re-declared in
every language that touches it and goes stale the first time it gains a member,
which is a cost paid by the frontend, the ingestion job, and anything written
in a language the app is not.

**A documented set of platform values still exists** — `QUEUED`, `RUNNING`,
`SUCCEEDED`, `FAILED`, `CANCELLED`, `INFEASIBLE` — as string constants rather
than an enforced type. They are a convention every model should reach for
first, not a wall.

> **The consequence, and the fix.** `TERMINAL_STATUSES` is how the harness
> knows to stop, the app knows to close a stream, and the client knows to stop
> waiting — and a `frozenset` of enum members cannot answer "is `CALIBRATING`
> terminal?" once any string is legal. Inferring terminality from a
> known-values list just rebuilds the closed enum somewhere less visible.
>
> So **`status` messages carry an explicit `terminal: bool`.** The producer
> says whether this is the last word on the run; nothing downstream infers it.
> That is strictly better than what it replaces — the property becomes
> *stated* rather than *derived from membership* — and it is the thing that
> makes an open status field safe rather than merely convenient.

Blast radius, measured: 38 non-test files reference `RunStatus` or
`TERMINAL_STATUSES` today. Most are mechanical, and the count is inflated by
the triple-copied `shared/` this plan already deletes.

### Encoding: JSON, everywhere

v3 used msgpack job→app and in the write buffer, JSON on the SSE stream. v4 is
**JSON throughout** — one codec instead of two.

It follows from two decisions rather than being a preference of its own. The
wire is JSON-RPC, and the durable records are files that **replay reads back**;
an operator opening a log file to see what a run did, and a `replay` that
parses the same bytes the wire carries, are both worth more here than smaller
frames. Telemetry records are small and the transport compresses. `shared/
codec.py` mostly disappears, and msgpack leaves both requirement sets.

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
/Volumes/main/dbx_leaning/telemetry/runs/<run_id>/…
```

A **separate scheduled or streaming ingestion job** reads those files and
loads them into SQL. That is a different job on a different schedule; it is
not on the live path.

#### Two volumes, and the grant matrix is the architecture

| Volume | Purpose | App | Job | Ingestion |
|---|---|---|---|---|
| `main.dbx_leaning.app_store` | The app's own files — exports, uploads, cached artefacts | **READ + WRITE** | — | — |
| `main.dbx_leaning.telemetry` | Run telemetry, written as it is produced | **none** | **READ + WRITE** | **READ** |

**The app has no grant on the telemetry volume, and that is the point.** "The app
never reads run telemetry from files" stops being a design principle someone
has to remember and becomes something Unity Catalog refuses. A future session
cannot take the shortcut, because the shortcut returns a permission error.
This is the strongest form of the separation and it costs one `CREATE VOLUME`.

Consequences worth stating explicitly:

- The app should not even be **configured** with the telemetry volume's path. No
  path, no grant, no temptation. `DBX_APP_VOLUME` keeps pointing at
  `app_store` and gains no sibling on the app side.
- The job needs `READ` as well as `WRITE`, because **replay reads back what it
  wrote** (below).
- The ingestion job is the only other principal with access, and only `READ`.

> **Naming.** `log` is narrower than what the volume holds: every envelope
> type lands here, so it carries `progress`, `status` and `result` summaries
> as well as `log` lines. `run_log` or `telemetry` would age better. Worth
> deciding now — renaming a volume later means touching grants, paths, the
> bundle and every written path prefix.
>
> It stays small either way. Result *rows* do not come here (below), so the
> volume holds a stream of small records and nothing bulk.

#### Who writes what: the model owns its own data

The harness does **not** write model output. A model reads its own inputs and
writes its own results table. The harness owns telemetry and comms, and
nothing else.

| What | Owner | Where | How |
|---|---|---|---|
| **Telemetry** — every envelope: `log`, `progress`, `status`, and the `result` *summary* | **Harness** | The `log` volume, as files | Plain file write, no Spark |
| **Model inputs** | **Model** | Unity Catalog | Its own Spark read |
| **Result rows** | **Model** | Its own UC results table | Its own Spark write |

This is what makes `job/delta.py` **redundant outright** rather than reduced —
there is no harness-side table write left for it to do, in any form. The
`results_table` config, `preview_axes` on the model handle, and
`emitter.py::_absorb_result_rows` go with it.

**`emit("result", ...)` changes shape accordingly.** A model no longer hands
rows to the harness to write. It writes them itself, then *reports*:
`row_count` (it knows what it wrote), `fetch_hint` (it knows where it put
them), and `preview` (a bounded sample it produces). The harness validates the
envelope and moves it; it never sees a result set. That is already what the
wire contract says — this just makes the harness honest about it, rather than
being a writer wearing a messenger's clothes.

`shared/downsample.py` (LTTB) stays useful and becomes a **utility a model may
call**, not a step the harness performs on the model's behalf.

> **The safety property this costs, stated plainly.** v3's rule was "a run must
> not report `SUCCEEDED` if its result write failed", and the harness could
> *enforce* it because the harness did the writing. It no longer can. The rule
> survives but moves into the **model contract**: a model whose write fails
> must raise or report failure, and the harness's remaining guarantee is
> narrower — a terminal `result` must carry a `row_count`, so "wrote nothing"
> stays distinguishable from "did not get that far".
>
> This is a real trade, not a free win. It is the price of the ownership
> boundary, and it is worth paying because a model that cannot be trusted to
> know whether its own write succeeded cannot be trusted with the write at all.
> It belongs in `job/models/README.md` as a contract requirement, not as
> advice.

> **Deferred, deliberately.** A heartbeat reads nothing and writes nothing, so
> Slices 0–3 never exercise this. It is designed, not built.

Consequences, all of them good:

- **Spark leaves the harness entirely.** Not just the telemetry path: with the
  model owning its own reads and writes, the *harness* has no Spark left in it
  at all. `job/delta.py` is 340 lines, most of it session archaeology — three
  acquisition strategies, a Spark Connect branch, and a thread-affinity bug
  fixed on Aug 27 — and all of it goes. Spark remains a **model** concern,
  inside the model, where the session it needs is the one it already uses to
  read its inputs.
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
> The probe runs against the new `telemetry` volume with the job's own principal,
> so it exercises the grant matrix at the same time.

### Backfill: the job replays from its own log

When a client reconnects mid-run and finds a gap, **the app asks the job to
resend it** — `replay(from_seq, to_seq)` — and the job re-reads its own log
file and sends those records over the WebSocket.

This is the design's keystone, and it is why RPC-over-WS is worth doing:

- The app **cannot read the telemetry volume** — it holds no grant on it — so this
  is not one option among several. It is the only live backfill path there is.
- No Files API dependency, no second credential path, no parsing the durable
  format in two places.
- The job is the authority on its own run while it is alive, and it already
  has the data on disk.
- Backfill stops being a warehouse query, which is the cost mistake this
  platform was built to avoid.

> **The consequence to accept deliberately.** For a **finished** run the job is
> gone, and the app still cannot read the telemetry volume — so history has exactly
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

### Run state: the job writes it, and there are two kinds

This is the part that changes most once a job is a service. A scheduled run
never touches the app, so **the app cannot be the writer of run state** — it
would be absent for exactly the runs that most need recording.

**The job maintains run state in Lakebase, and keeps it current.** Two
distinct things live there, and conflating them is how this gets muddled:

| | What it is | Who defines the values |
|---|---|---|
| **Job status** | The platform lifecycle — running, succeeded, failed, cancelled | The platform. Fixed, small, shared by every model |
| **Model status** | Where this particular model thinks it is — its own categorical stages | **The model.** Varies per model by design |

Reading it back splits three ways, and each has exactly one answer:

- **"Is it running?" → the Jobs API.** Not a table, not a count. The platform
  already knows, authoritatively, and it cannot drift.
- **"What is it doing right now?" → the app's in-memory cache**, fed by the
  live stream. Latest value only, no history, gone on restart — and that is
  fine, because it is a cache of something durable.
- **"What has it been doing?" → Postgres.** Categorical progress, history,
  anything a chart needs across time.

> **This retires v3's count-and-claim transaction, and that is a
> simplification worth naming.** v3 wrapped a count and an insert in one
> transaction so the 5-task ceiling would be "real rather than advisory".
> Under this model that machinery is both unnecessary and slightly dishonest:
> **Databricks enforces the ceiling itself**, and a scheduled run that never
> passes through the app was never counted anyway. So the app asks the Jobs
> API what is actually running, and the Postgres row goes back to being a
> **record rather than a lock**. Less code, and it stops being wrong the
> moment a run starts without the app.

**The cost, stated:** the harness gains a Postgres client and a Lakebase
credential. That is a real dependency and it is worth being precise about what
kind — `psycopg` is an ordinary database driver, not pyspark, Delta or Unity
Catalog. A service owning its own state row in a shared database is the normal
microservice shape; a harness reaching into a lakehouse is not. So the earlier
claim needs restating: **the harness has no *lakehouse* dependency**, which is
what makes it portable, and it does have a database one, which is what makes
it a service.

> **Open, and the one thing here that is not decided:** where model status
> lives on the wire. `shared/envelope.py` has `status: RunStatus` — a fixed
> six-value enum — plus a free-text `detail`. A model-defined categorical
> status is neither. Cheapest option that keeps Postgres to one row per run:
> a `model_status` column carrying the latest value, with the *history* riding
> the ordinary telemetry stream as `progress` messages, where per-model
> free-form data already lives in `payload`. That needs deciding before the
> first real model, not before the heartbeat.

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

**Slice 0 — create the `telemetry` volume, then one probe. Written; not yet
run.** The artifacts exist:

| | |
|---|---|
| `uc_ddl/004_telemetry_volume.sql` | The volume and its grants. **Fill in the placeholder principals before applying** |
| `scripts/probe_volume_append.py` | The probe. Runs both strategies, judges each on four criteria, reports latency |
| `resources/probe_volume.job.yml` | Runs it as a job, because the question has no answer off a workspace |

```bash
databricks sql query --warehouse-id "$DBX_WAREHOUSE_ID" --file uc_ddl/004_telemetry_volume.sql
databricks bundle deploy -t dev
databricks bundle run probe_volume -t dev
```

The probe passes a strategy only if all four hold: writes do not raise; **a
second reader opening the path fresh mid-run sees data already written**;
bytes read back are identical to what went in; and nothing is lost or
reordered across the run. "It did not raise" is explicitly not enough — this
repo has already shipped a write that succeeded into the wrong place.

It also reports write latency, which decides flush cadence. **If a small write
costs ~200 ms on FUSE, the write-through design is dead** and some form of the
buffer this plan deletes has to come back. Better learned here than in Slice 1.

**Paste the output into this file when it runs.** Slice 0 is not finished when
the probe passes; it is finished when the answer is written where the next
session reads it — which is the discipline `docs/spike-results.md` exists to
enforce, and the one v3 half-kept (both probes passed, none of the timings
recorded).

**Slice 1 — heartbeat, end to end.** A "model" that emits a tick a second and
nothing else, ~50 lines. Job (threaded) → RPC over WS → app → SSE → browser,
with records landing in the volume as they are produced and the job keeping its
own state row current in Postgres. **Deployed, not local.** When a tick appears
in a browser from a deployed job and the files are in the volume, the platform
exists.

**The client for this is new and deliberately tiny** — a page that renders
ticks and nothing else. v3's SPA is not stripped down for it and is not ported;
it stays on `dev`, intact and runnable. The point of heartbeat-first is that
when something breaks it is the transport, not a UI. `transport/hub.ts` is
worth re-reading before writing the new one — its consecutive-failure counter
(reset on every successful open, cap 10) and its gap detection are hard-won and
the reasoning applies unchanged — but re-read, not lifted, because it is built
around v3's four channels.

**Slice 2 — cancel, acknowledged.** Browser → app → job → ack → terminal
status. The first thing the RPC interface buys.

**Slice 3 — replay.** Cut the connection mid-run, reconnect, call
`replay(from_seq, to_seq)`, get the gap filled from the job's own log. Then
kill the app entirely and confirm the run completes and its files are intact.
That is the autonomy invariant tested rather than asserted.

**Slice 4 — one real model, then the ingestion job (volume → SQL).** The
model comes first: a heartbeat never exercises result reporting, model status,
or a run long enough to meet the ingress's real timeouts, and all three gate
"the job side is stable". The ingestion job is promoted out of "later" by the
grant matrix: with the app locked out of the telemetry volume, this is the *only*
way a finished run becomes visible. It is not on the live path and it is not
urgent for Slices 1–3, but the platform is not usable without it, so it should
not drift.

**Only once all of that is deployed and boring** — which is the same bar as
"the job side is stable", below — does anything else start: the app's language
migration, more models, and last of all the question of whether any model has
*earned* a bespoke view.

## The language decision, deferred on purpose

**Scope: the app/backend only. The job stays Python.** The models are Python
and always will be — Gurobi, OR-Tools, torch — and each reads its inputs and
writes its results through Spark, so **pyspark stays in the model layer.**
What changes is that it is no longer anywhere in the *harness*: with the model
owning its own data, the harness is a comms and telemetry layer with no
Databricks data dependency at all.

That is also what would make a future split into separate services cheap. The
harness's contract becomes "run this object, move its messages, write its
telemetry, keep its state row current" — tied to no lakehouse component at
all, and to Postgres only through an ordinary driver.

That leaves the app as the only open question. Its job is narrow — an RPC
endpoint for jobs, SSE fan-out to browsers, Postgres for run state, a few REST
calls to the Jobs API, and serving a static SPA. Nothing in that forces
Python.

### Two constraints that narrow the field before taste does

Both were found by reading the deployed configuration, and the second is the
one that surprises people.

**1. Databricks Apps has no build step, and a Python-shaped runtime.**
`app/app.yaml` carries a generic `command:` array — today
`uvicorn server.main:app --host 0.0.0.0 --port $DATABRICKS_APP_PORT` — and the
platform installs `requirements.txt`. The failure message when it is missing
is *"No command to run and no Python file found"*, which says plainly what the
paved path is. **A compiled binary would have to be committed pre-built** for
the Apps container's architecture, because nothing in the workspace will
compile it.

That is not automatically a blocker, and there is a precedent right here:
`app/dist/` — the built SPA — is committed for exactly this reason, because
"a deploy driven from inside Databricks has no Node runtime and sees only
tracked files". A Go binary would follow the same pattern. But whether Apps
will *run* one is **unverified**, and this project has been burned three times
by unverified platform assumptions. It is a probe, not an assumption.

**2. The `databricks-sdk` decision only holds for some languages.** This plan
adopts the SDK for token acquisition and refresh, on the evidence that
hand-rolling it cost 343 lines and two deploy bugs. Official SDKs exist for
**Python, Go and Java**. Choosing **Rust** — or anything without one — silently
reverses that decision and puts the OAuth code back.

### The field, given those

| Option | SDK | Apps fit | What it actually teaches |
|---|---|---|---|
| **Python, different framework** — Litestar, bare Starlette, Granian | Yes | Paved path | Architecture only. The RPC dispatch, threading model and durability design are the whole lesson; the syntax is not |
| **Go** | Yes | Needs the binary probe | Genuine concurrency for a fan-out relay, one static binary, and a real test of the Apps runtime. The strongest "different tech" candidate that does not undo another decision |
| **Rust** — axum | **No** | Needs the binary probe | The most learning per line, and the most yak-shaving: no SDK, so auth is hand-rolled again, plus the binary question |
| **Node / TypeScript** | No official one | Likely supported | Shares a language with the client, which is a real ergonomic win. But no SDK, so the same auth reversal as Rust |

**Recommendation, not a decision:** if the goal is to try different tech
without re-litigating settled choices, **Go is the one that costs least and
teaches most** — it keeps the SDK, suits a WS/SSE relay, and its one risk is
answerable by a probe (deferred — see below). Python-with-a-different-framework
is the honest answer if the appetite is for architecture rather than language.

### Deferred until the job side is stable

**The app stays Python and FastAPI until then.** Not as the final answer — as
the way to keep one variable moving at a time. The job is being rewritten in
almost every dimension at once: threaded instead of asyncio, RPC instead of
one-way frames, volume files instead of Delta, its own Postgres state, no
Spark. Rewriting the app in an unfamiliar language *at the same time* means
every failure has two candidate causes, and the one being learned is the one
that gets blamed.

It is also the discipline this repo already has, twice: `shared/` was frozen
before the tracks fanned out, and `contract.ts` was frozen before the
per-model views. **Freeze the contract, then rebuild against it.** Rewriting
the app while the RPC surface is still moving is chasing a live contract in a
language you are still learning.

What the app needs meanwhile is small and worth doing in Python regardless,
because it is the same work in any language: an RPC endpoint the job connects
to, the warehouse read path deleted, JSON instead of msgpack, and run state
read rather than owned.

#### What "stable" means, so this deferral has an end

"Stable" is exactly the kind of word that never resolves. It is met when all
of these are true:

1. **Slices 0–3 are deployed and behaving** — heartbeat streaming, cancel
   acknowledged, replay filling a real gap, and a run completing with the app
   deliberately killed.
2. **The telemetry write strategy is settled** — the append probe answered,
   and rolling part files either adopted or ruled out.
3. **The job writes its own run state**, and the app reads it without
   repairing it.
4. **The RPC method surface has stopped changing.** This is the real gate. The
   others can wobble; this one cannot, because it is the contract the rewrite
   is built against.
5. **At least one real model has run through it end to end** — a heartbeat
   never exercises result reporting, model status, or a run long enough to
   meet the ingress's actual timeouts.

Only then: run the binary probe, pick the language, rewrite the app against a
contract that is no longer moving. The binary probe is therefore **not** part
of Slice 0 — probing a runtime for a decision months away is effort spent to
answer a question nobody is asking yet.

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
