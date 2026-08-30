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

**Rule for v4: no document may claim a deployment status. It goes stale
faster than anything else in the repo, and it is the one fact every session
reads first.** Status belongs in git, in the bundle, and in the workspace.

## The measurements this plan is built on

Taken 2026-08-30 against `dev`. `uv sync --all-extras && uv run pytest` →
**952 passed, 1 skipped in 45s**.

| Layer | Lines | Verdict |
|---|---|---|
| `shared/` — envelope + protocol | 900 | Keep. Lean and correct |
| `job/` harness (excl. models) | ~2,800 | Rewrite the transport and durability; keep the model contract |
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

Ported, not redesigned. Each of these survived eleven models and a real
deployment without changing shape.

- **`shared/envelope.py`** (290 lines). `seq`, `row_count`, `chunk_index` and
  `final` each exist because something broke without them; `row_count` is the
  only thing distinguishing "succeeded, wrote nothing" from "succeeded, wrote
  8,760 rows". Do not re-derive this. Port it and put a new transport under it.
- **The duck-typed model contract.** `docs/parallelization-plan.md` records
  six models added with zero harness changes, and `ortools_jobshop` surfacing
  a real cancellation gap that was fixed *inside the model's own callbacks*
  rather than with a new harness hook. That is the contract working.
- **The autonomy invariant.** The job runs; the app watches when it can. Apps
  die at 24h, jobs do not. Everything else follows from this and it is not
  up for revision.
- **`shared/downsample.py`** — LTTB. Small, correct, and stride-sampling
  hides exactly the spikes that matter.
- **The deployment lessons below.** These are the most valuable thing in the
  repo and the easiest to lose in a rewrite.

### Deployment lessons that must not regress

Extracted from the Aug 25–27 shakeout. A v4 that rediscovers these has wasted
the only thing v3 bought that v1 and v2 did not.

| Lesson | Where it bit |
|---|---|
| Serverless does not set `__file__` | Repo-root discovery |
| A `spark_python_task` runs inside an ipykernel that **already has a running event loop** — `asyncio.run` refuses | `job/main.py::_run` |
| The SQL warehouse is usually **asleep**; a query after any quiet period waits for it to start, and a naive timeout cancels your own statement | `app/server/sql.py` |
| A job needs **two** credentials on different headers — an OAuth identity for the Apps proxy, a shared secret for the app | `job/auth.py` |
| The job→model id map may be absent from the environment and must be discoverable from the workspace | `app/server/discovery.py` |
| Write against the **table's** schema, never one inferred from the batch | `job/delta.py` |
| A workspace secret scope is not a Unity Catalog object | `deploy/README.md` |

## What gets deleted

- **The ten non-heartbeat models**, archived on `dev` and out of v4's tree
  until the platform is proven without them. Eleven models is the reason the
  platform cannot be experimented on: every transport change has 22
  downstream consumers, and each new one costs a job file, a requirements
  export, a DDL block, a registry entry and ~1,800 lines of React.
- **All per-model frontend views** — 20,117 lines. The generic view returns.
- **`WarehouseRunStore`** and the `RunStore` Protocol. It exists only so a
  deploy is never blocked on provisioning. Lakebase is provisioned.
- **`app/server/sql.py` + `repository.py`** (672 lines) — see durability below.
  If telemetry is read from volume files, the app never needs the warehouse.
- **`DeltaRsWriter`.** It was kept as a named class to hold an interface open.
  The interface is being replaced; let it go.
- **The triple copy of `shared/`.** `shared/`, `app/shared/`, `job/shared/`,
  plus a sync script and a drift test to make the duplication safe. v4 either
  packages it as a wheel or accepts one copy — not three.

## The v4 architecture

### Transport: one channel, RPC-shaped

v3 carries one envelope over **four paths with four different failure
semantics** — WS, HTTP push, Delta, SSE. That, not `protocol.py` (114 lines),
is the actual over-build.

v4: **JSON-RPC 2.0 over the WebSocket**, one bidirectional channel, methods
dispatched from one table instead of a `ControlKind` if-chain.

What this buys that v3 lacks — concretely, not aesthetically:

- **Cancel gets an acknowledgement.** Today `_on_control` sets a token and
  replies nothing, so the app cannot distinguish "cancel delivered" from
  "cancel lost". A request/response pair fixes that.
- **The job can ask the app things**, not only tell it. Config, whether it is
  still wanted, whether anything is listening.
- **Errors have one shape** rather than a per-call convention.

> ### ⚠️ Not gRPC, unless it is probed first
>
> gRPC needs **HTTP/2 end-to-end plus trailers** through the Databricks Apps
> ingress. `docs/spike-results.md` cleared WebSocket `Upgrade` and SSE. It did
> **not** clear HTTP/2 gRPC, which is a materially different question of the
> proxy.
>
> Betting a rewrite on an unverified ingress assumption is the exact mistake
> that produced v1 (avoided WebSockets on hearsay) and v2 (shipped
> feature-complete with "verify the WebSocket survives the ingress" as
> unfinished item #1, never deployed). **JSON-RPC over WS gets the semantics
> with none of the risk**, riding an `Upgrade` already proven to work.
>
> If gRPC is wanted for its own sake — a legitimate reason on a learning
> project — probe it in a throwaway **before** it is load-bearing. It is a
> half-day and it protects the whole build.

### Durability: files to a volume, no Spark

The job stops writing Delta and writes **files to a Unity Catalog volume**:

```
/Volumes/<catalog>/<schema>/<vol>/runs/<run_id>/part-NNNNN.jsonl
```

Ingestion from volume → Delta tables is **deferred**, and is a scheduled job
or Auto Loader later. It is not on the live path and does not block anything.

One correction to the framing this came from: **the job already never touches
the SQL warehouse** — zero references in `job/`. v3's write path is Spark →
Delta. So the real prize here is not avoiding the warehouse; it is:

- **Spark leaves the job entirely.** `job/delta.py` is 340 lines, most of it
  session archaeology: three acquisition strategies, a Spark Connect branch,
  and a thread-affinity bug fixed on Aug 27. A file append needs none of it.
- **Concurrency stops being a question.** Each run owns its own directory, so
  there is nothing to conflict — strictly simpler than Delta's optimistic
  concurrency and its S3 locking caveat.
- **The app may drop the warehouse altogether.** With `run_status` in
  Postgres and telemetry in volume files, nothing on the live path needs it:
  672 lines and the warehouse-start race go with it.
- **A model environment gets much smaller.** No pyspark in the job floor.

Retained rules, unchanged: flush on **size OR age OR end-of-run** (the age
bound is what caps loss on a crash), and **a run must never report
`SUCCEEDED` over a failed durable write**.

> **Probe before committing:** that the app can *read back* volume files via
> the Files API (`/api/2.0/fs/files{path}`, `/api/2.0/fs/directories{path}`)
> with the service principal it already has. Backfill depends on it. This repo
> has paid twice for assuming a read path works — `hour_ts` typed by
> inference, and delta-rs silently writing a three-part UC name to a local
> directory. Verify, then build.

Known trade-off, accepted: many small files. Fine at this scale; the
volume→Delta ingestion step is where it gets fixed if it ever matters.

### Run state: Lakebase Postgres

Unchanged in design, and already built. `run_status` is the one OLTP-shaped
thing here — a primary key on `run_id`, and a transaction around
count-and-claim that makes the 5-task ceiling real rather than advisory.
Delta cannot do either.

### Auth: the SDK, for credentials only

`CLAUDE.md` currently states the `databricks-sdk` is "deliberately absent from
every dependency set." **That rule was half right, and the wrong half cost
343 lines** (`job/auth.py` 193, `app/server/oauth.py` 150) plus two of the
Aug 27 fixes.

v4 splits it where the value actually is:

- **SDK for token acquisition and refresh.** Nobody should hand-roll OAuth
  token exchange, and this project's own history is the evidence.
- **Plain `httpx` for the Jobs and Files APIs.** The original reasoning holds:
  they are a few REST calls, and the SDK's weight would otherwise be paid by
  every model environment.

## Build order — vertical slices, heartbeat first

The ordering principle, and the one most different from v3: **prove comms
with no model in the picture.**

**Slice 0 — probes.** Files API read-back. gRPC over the ingress *only if*
gRPC is still wanted. Half a day, and everything downstream rests on it.

**Slice 1 — heartbeat, end to end.** A "model" that emits a tick a second and
nothing else, ~50 lines. Job → JSON-RPC over WS → app → SSE → browser, plus
files landing in the volume. Deployed, not local. **This is the milestone
that matters**; when a tick appears in a browser from a deployed job and the
files are in the volume, the platform exists.

**Slice 2 — cancel, with an acknowledgement.** Browser → app → job → ack →
terminal status. The first thing JSON-RPC buys, and the first thing v3 cannot
do.

**Slice 3 — durability under failure.** Kill the app mid-run; the run must
complete and be fully readable afterwards. Backfill from volume files. This
is the autonomy invariant, tested rather than asserted.

**Slice 4 — one real model.** `gurobi_scheduling` or `mcmc`. One. Generic
view only.

**Slice 5 — the generic view, properly.** Make it good enough that model
number two needs no frontend work at all. That is the test the thesis failed
in v3, and it is worth passing deliberately.

Only then: a second model, and the question of whether any model has *earned*
a bespoke view.

## The language decision, deferred on purpose

Settle the architecture above first, then pick. The criteria, so it is a
decision and not a preference:

- **Models are Python and always will be** — Gurobi, OR-Tools, torch. That is
  fixed regardless.
- The volume-file durable path **removes Spark from the job**, which is what
  makes a non-Python app or harness genuinely viable. Under v3's Spark
  dependency it was not.
- So the live options are: keep Python and change its shape (framework,
  concurrency); or split — a compiled app and relay, Python models behind a
  process boundary.
- **What a learning project should optimise for is which one teaches more per
  unit of yak-shaving**, and that is easier to judge once the transport shape
  is fixed.

## What "done" means for v4

The bar v3 met, plus the two it did not:

1. A deployed run streams to a browser. *(v3: met)*
2. A run with no app listening is fully durable and readable afterwards.
   *(v3: designed for, never adversarially tested)*
3. **Adding a model costs no frontend code.** *(v3: failed, at ~1,800 lines
   each)*
4. **The docs are true on the day they are read.** *(v3: failed, and it is
   why this document opens the way it does)*
