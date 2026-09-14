# Architecture diagrams

Visual companion to `docs/architecture.md` and `docs/message-envelope-spec.md`
— those two remain the source of truth for *why*; this file is *what talks to
what*, drawn from the actual code (`app/server/`, `job/`, `shared/`) rather
than from the target layout in `CLAUDE.md`. If a box or arrow here disagrees
with either of those, they win — file an update here.

Solid arrows are the **live** path (best-effort, may be down for hours at a
time). Dashed arrows are the **durable** path (Delta, always runs) or an
infrequent/on-demand call. Dotted arrows are control-plane calls (triggering
or querying a job run, not part of either transport tier).

## Component / data-flow diagram

```mermaid
flowchart TB
    subgraph Browser["Browser"]
        SPA["app/dist/index.html<br/>hand-written SPA, no build step<br/>EventSource + fetch"]
    end

    OIDC["OIDC token endpoint<br/>/oidc/v1/token<br/>(a Databricks REST API, not a system of its own)"]

    subgraph App["Databricks App — app/server/ (FastAPI, async)<br/>up to 24h per deploy, ~8h/day in practice"]
        Meta["routes/meta.py<br/>healthz · whoami · /api/schema"]
        RunsRoute["routes/runs.py<br/>POST run-now · cancel · replay/backfill"]
        StreamRoute["routes/stream.py<br/>GET SSE (Last-Event-ID resume)"]
        RpcRoute["routes/rpc.py<br/>WS endpoint, one socket per run_id"]

        subgraph Hub["ServiceHub (app.state, built once in lifespan)"]
            Broadcaster["Broadcaster<br/>InProcessBroadcaster<br/>RunSnapshot per run_id"]
            JobConn["JobConnections<br/>live WS per run_id + pending RPC futures"]
            Store["PostgresRunStore<br/>set_status() upsert · list_runs() · get()<br/>no launch-time gating — see notes"]
            JobsApiC["JobsApi client<br/>run_now / get_run / terminal_status()"]
            OAuthC["OAuthTokenProvider<br/>M2M client-credentials"]
        end
    end

    subgraph JobTask["Databricks Job task — one serverless task per run<br/>5-concurrent account ceiling: Databricks' own queue.enabled holds it"]
        Harness["Harness (job/harness.py)<br/>3 threads: main / roller / socket"]
        Loader["loader.py<br/>importlib.metadata entry point<br/>DBX_MODEL name -&gt; class/object"]
        Model["Model (duck-typed, models/*)<br/>heartbeat, annealing, ...<br/>no imports from job/ or shared/"]
        RpcClient["RpcClient (job/ws.py)<br/>socket thread, backoff on consecutive failures"]
        PartWriter["PartFileWriter (job/telemetry.py)<br/>roller thread"]
        CancelToken["CancellationToken<br/>threading.Event"]
        Secrets["auth.py::read_secret<br/>dbutils.secrets.get"]
    end

    subgraph UC["Unity Catalog"]
        Volume[("Telemetry volume<br/>/Volumes/.../telemetry/runs/&lt;run_id&gt;/part-NNNNN.jsonl<br/>durable once a part is CLOSED")]
        DeltaCore[("Delta core tables (job-authored, uc_ddl/001_core_tables.sql)<br/>run_logs · run_progress<br/>run_events — append-only status transitions,<br/>THE authoritative status record")]
        DeltaResults[("Delta per-model results tables<br/>uc_ddl/002_model_results.sql")]
    end

    subgraph Warehouse["SQL Warehouse — 2X-Small, single, billed on uptime"]
        SEA["Statement Execution API<br/>httpx, infrequent/read-only only"]
    end

    subgraph Lakebase["Lakebase (managed Postgres)"]
        RunStatus[("run_status<br/>PK run_id · one row per run<br/>lakebase_ddl/001_run_status.sql")]
    end

    %% ---- browser <-> app ----
    SPA <-->|"SSE, JSON, one-way<br/>Last-Event-ID resume"| StreamRoute
    SPA -->|"POST /api/runs<br/>run-now / cancel / backfill"| RunsRoute
    SPA -.->|"GET /healthz, /whoami, /api/schema"| Meta

    %% ---- app internals ----
    RunsRoute -.->|"list_runs() / get(): read-only,<br/>for GET /api/runs and GET /api/runs/{id}"| Store
    RunsRoute --> JobsApiC
    RpcRoute --> JobConn
    JobConn --> Broadcaster
    Broadcaster -->|"fan out to every SSE<br/>subscriber of this run_id"| StreamRoute
    RpcRoute -.->|"ingest(): mirrors the latest status into run_status<br/>via an upsert — a best-effort CACHE for fast<br/>point-lookups &amp; listing. NOT authoritative: run_events<br/>is. Creates the row lazily; nothing reserves it at launch"| Store

    %% ---- app -> job, via the Jobs REST API (not drawn as its own system —<br/>       it's a plain Databricks API call, same as any other) ----
    JobsApiC ==>|"run_now(job_id, job_parameters) via Jobs API<br/>-&gt; starts a new serverless task<br/>DATABRICKS_HOST as a task parameter"| Harness
    JobsApiC -.->|"get_run() -&gt; terminal_status() via Jobs API<br/>on demand, not a poll loop"| Harness
    OAuthC <-->|"client-credentials"| OIDC
    JobsApiC -.-> OAuthC

    %% ---- app <-> lakebase ----
    Store <-->|"parameterised SQL, bound params<br/>set_status(): INSERT ... ON CONFLICT DO UPDATE —<br/>the upsert that creates a row on first write"| RunStatus

    %% ---- job internals ----
    Harness --> Loader --> Model
    Model -->|"emit(type, **fields)<br/>the model's only coupling to the platform"| Harness
    Harness --> RpcClient
    Harness --> PartWriter
    Harness --> CancelToken
    RpcClient --> Secrets
    Secrets -.->|"M2M token exchange"| OIDC

    %% ---- job <-> app (live, bidirectional) ----
    RpcClient <-->|"WS, msgpack<br/>job-&gt;app: hello, telemetry notifications<br/>app-&gt;job: cancel, replay, ping"| RpcRoute

    %% ---- job -> UC (durable, always) ----
    PartWriter -->|"append; roll on<br/>size≥1MB OR age≥30s OR EOF"| Volume
    PartWriter -.->|"Spark write_batch()<br/>on every roll"| DeltaCore
    PartWriter -.-> DeltaResults

    %% ---- app -> UC (infrequent reads only) ----
    RunsRoute -.->|"backfill a gap on request<br/>(never a timer)"| SEA
    SEA -.-> Volume
    SEA -.-> DeltaCore
    SEA -.-> DeltaResults

    classDef browser fill:#e8f1ff,stroke:#5b8def,color:#1a1a1a;
    classDef app fill:#e6f7ee,stroke:#2fa86b,color:#1a1a1a;
    classDef hub fill:#d3f0e0,stroke:#2fa86b,color:#1a1a1a,stroke-dasharray: 3 2;
    classDef job fill:#fff2e0,stroke:#e08a2b,color:#1a1a1a;
    classDef uc fill:#f1e8ff,stroke:#8a5bd6,color:#1a1a1a;
    classDef wh fill:#f2f2f2,stroke:#888,color:#1a1a1a;
    classDef lb fill:#e0f7f7,stroke:#2b9c9c,color:#1a1a1a;
    classDef ctrl fill:#f2f2f2,stroke:#888,color:#1a1a1a;

    class SPA browser;
    class Meta,RunsRoute,StreamRoute,RpcRoute app;
    class Broadcaster,JobConn,Store,JobsApiC,OAuthC hub;
    class Harness,Loader,Model,RpcClient,PartWriter,CancelToken,Secrets job;
    class Volume,DeltaCore,DeltaResults uc;
    class SEA wh;
    class RunStatus lb;
    class OIDC ctrl;
```

Reading it:

- **Only two arrows cross the App/Job boundary while a run is live**: the WS
  (bidirectional, both directions best-effort) and the Delta write (one-way,
  never best-effort). Everything else in the `App` box is internal wiring.
- **The SQL warehouse touches nothing on the live path** — its only edges are
  dashed and originate from an explicit user action (backfill a gap), never a
  timer. That absence is the point of `docs/architecture.md`'s warehouse
  section, not an omission.
- **`ServiceHub` is composition, not a data path** — it's drawn as a nested
  box because every one of `Broadcaster`, `JobConnections`, `PostgresRunStore`,
  `JobsApi` and `OAuthTokenProvider` is built once in `lifespan` and reached by
  the routes via `Depends`, never imported at module scope.
- **A model (`models/*`) has exactly one edge on this whole diagram**: `emit()`
  into the harness. It never appears connected to `UC`, `Lakebase`, or any
  transport — that's `job/loader.py` discovering it by entry point, not by
  inheritance.
- **The job → app WS is one connection carrying two directions of traffic**:
  telemetry notifications flow job→app continuously; `cancel`/`replay`/`ping`
  requests flow app→job on the same socket, answered by
  `JobConnections`'s pending-future bookkeeping.
- **`run_events` (Delta, job-authored) is authoritative for status — `run_status`
  (Lakebase) is not.** The job writes every status transition into `run_events`
  through the same unconditional telemetry path as everything else
  (`PartWriter` → `Spark write_batch()`), whether or not the app is listening.
  `run_status` only exists because `run_events` is the wrong *shape* for what
  the app needs live — a point lookup by `run_id` for listing and reading runs
  — so the app keeps its own one-row-per-run mirror, best-effort, updated when
  a WS status message happens to arrive. The job has no Postgres/Lakebase code
  at all (`app/shared/tables.py` says this outright); it never writes that
  mirror.
- **Nothing on this diagram enforces the account's 5-concurrent-task ceiling
  — and that's correct, not an omission.** An earlier design had
  `PostgresRunStore.claim_slot()` do an atomic count-and-claim before every
  launch; it is dead code today (nothing calls it — confirmed by grep, and
  `routes/runs.py::trigger_run()`'s own docstring says so: *"this is the
  change from v3... The ceiling still holds; Databricks holds it. Every job
  file sets `queue.enabled`, so a sixth concurrent task waits instead of
  failing."*). Every `resources/*.job.yml` does set `queue: enabled`. See
  `docs/v4-rewrite-plan.md`'s "Run state: the job writes it, and there are two
  kinds" for the fuller reasoning this retired.
- **The Jobs API and the OIDC token endpoint aren't drawn as boxes.** Both are
  plain Databricks REST calls the app happens to make (`run_now`/`get_run`,
  and the M2M token exchange) — treating them as their own "control plane"
  system implied a component that isn't there. The diagram shows their effect
  directly: `run_now` starts the task, `get_run`/`terminal_status()` answers a
  question about it, and the OIDC edge is just how a token gets minted.

## Run lifecycle (sequence)

The same system, as one run's timeline — including the case that matters
most for this platform's cost model: **the app is not watching**.

```mermaid
sequenceDiagram
    autonumber
    actor U as Browser
    participant A as App (routes/runs.py)
    participant P as Lakebase (run_status, app's cache)
    participant J as Job task (Harness)
    participant M as Model
    participant V as Telemetry volume (UC)
    participant D as Delta tables (UC)

    U->>A: POST /api/runs {model}
    Note over A,P: trigger_run() does not touch the store at all —<br/>no slot claimed, no row inserted. If the account is already<br/>at the concurrency ceiling, Databricks queues this task itself<br/>(every job resource sets queue.enabled) instead of the app<br/>refusing the request.
    A->>J: run_now(job_id, job_parameters) via Jobs API<br/>starts a new serverless task
    Note over J: Harness loads Model by entry point,<br/>spawns roller + socket threads.<br/>Main thread blocks running the model — exactly as a solver wants to be.
    par best-effort, may never succeed
        J->>A: WS connect + hello(seq=last committed)
        A-->>U: SSE: status RUNNING (if a browser is subscribed)
    and always, regardless of the WS above
        loop model runs
            M->>J: emit(log / progress / result)
            J->>V: append to part-NNNNN.jsonl
        end
    end
    opt WS is connected
        J-->>A: telemetry notifications (msgpack, batched)
        A-->>U: SSE events (JSON)
    end
    Note over V,D: a part is durable only once CLOSED —<br/>roll fires on size≥1MB OR age≥30s OR end-of-run
    V->>D: Spark write_batch() on every roll
    opt user cancels, and a live channel exists
        U->>A: WS cancel (never a warehouse poll)
        A->>J: RPC cancel via JobConnections
        J->>M: CancellationToken.set()
        M-->>J: stops at next checkpoint, keeps its incumbent result
    end
    J->>V: final result + terminal status, part closed
    V->>D: last flush: run_events gets the terminal<br/>status row. THIS is authoritative, regardless<br/>of anything below.
    opt WS still connected
        J-->>A: status message (terminal)
        A->>P: set_status(run_id, terminal_status)<br/>— an upsert: creates the row if this is the<br/>first message the app ever saw for this run
        A-->>U: SSE: status terminal
    end
    Note over A,J: If the WS was never up, or dropped and stayed down,<br/>none of the opt-WS-is-connected steps happen — P may never even<br/>learn this run existed. The run still finishes and D still has the<br/>true terminal status either way,<br/>the job never depended on the app to make its own status authoritative.<br/>JobsApi.get_run()/terminal_status() can also answer did-it-finish<br/>on demand from the Jobs API, which cannot go stale by construction.
```

Reading it:

- Everything inside the `par` block's second branch (model → job → volume →
  Delta) happens **unconditionally**. The WS branch alongside it is drawn as
  `par`, not `then`, on purpose — it is not a step the durable path waits for
  or depends on.
- The cancel step is the concrete shape of "cancel goes through the app,
  never a warehouse poll" (`docs/architecture.md`): the client's only inbound
  channel is the WS, and if it isn't up, there is currently no live cancel
  path — the escape hatch is a direct `databricks jobs cancel-run`
  (`app/server/jobs_api.py`'s `CANCEL_ESCAPE_HATCH` reference), not a warehouse
  flag a poller would pick up.
- `run_now` is drawn straight from the app to the job task: the Jobs API is
  the mechanism by which Databricks starts that task, not a separate system
  worth its own lane on this diagram.
- **There used to be a `claim_slot()`/`SlotDenied` step here, and it's gone
  because it was never actually called.** `routes/runs.py::trigger_run()`
  launches a run without touching the store at all — confirmed by grep,
  nothing in `app/server/` calls `claim_slot()`, `attach_job_run()`, or
  `release_slot()`. Its docstring says this is deliberate: a scheduled run
  never passes through the app, so a ceiling checked only on this one route
  was already counting the wrong number, and every job resource's
  `queue.enabled` makes Databricks itself the thing that queues a run past
  the account ceiling. `docs/v4-rewrite-plan.md` has the fuller argument.
- The terminal `set_status()` call into `P` (Lakebase) is labelled as a cache
  refresh, not a source of truth — because it isn't one. The job's own write
  into `D`'s `run_events`, one step earlier, is what makes the terminal status
  real; it happens whether or not the app, the WS, or Lakebase are anywhere
  in the picture. One consequence worth knowing: since nothing calls
  `attach_job_run()` any more, `run_status.job_run_id` is never populated —
  the column exists but nothing currently writes it.
- The final note is deliberately hedged: `JobsApi.get_run()` and
  `terminal_status()` exist and can answer "did this finish?" for a
  `job_run_id` on demand, but as of this writing `app/server/main.py`'s
  `lifespan` does **not** call them automatically at startup to reconcile
  stale `run_status` rows — an earlier warehouse-based reconciliation step was
  removed and nothing has replaced it yet. Don't read this diagram as saying
  otherwise; check `app/server/main.py` and `app/server/services.py` before
  relying on automatic reconciliation existing.

## Proposed: job-authored `run_status`, over the Lakebase Data API (not yet built)

Everything above is the shipped design: the app writes `run_status` from a
WS status message it happens to receive (`services.py::_persist_status`),
and `job/` has no Postgres code at all. This section is a **proposal**,
kept deliberately separate from the two diagrams above so neither one
misrepresents what's actually running on `v4-plan` today.

**This isn't a new idea — it's already the decision record.**
`docs/v4-rewrite-plan.md`'s "Run state: the job writes it, and there are two
kinds" section worked this out on 2026-08-30: *"A scheduled run never
touches the app, so the app cannot be the writer of run state — it would be
absent for exactly the runs that most need recording... The job maintains
run state in Lakebase, and keeps it current."* What follows restates that
plan against the diagrams above, with one mechanism updated (Lakebase Data
API instead of a Postgres driver) and one open question narrowed.

### Three status concepts, not two

The plan doc's own table is the clearer cut than a Databricks-vs-ours split:

| | Job status | Model status |
|---|---|---|
| What it is | The platform lifecycle: `QUEUED`/`RUNNING`/`SUCCEEDED`/`FAILED`/`CANCELLED`/`INFEASIBLE` + `detail` | Where *this particular model* thinks it is — its own categorical stages |
| Defined by | The platform. Fixed, small, shared by every model | The model. Varies per model by design |
| Wire shape today | `shared/envelope.py`'s `status: RunStatus` | Still open — see below |

...and a third question that neither row answers, because it isn't about
`run_status` at all: **"is the container still alive?"** — that's
`life_cycle_state`/`result_state`, Databricks' own concept, needing no write
from us, read straight from `GET /api/2.2/jobs/runs/get`
(`JobsApi.get_run()`) whenever it's actually asked. The plan doc states the
three-way split on "reading it back" directly: *"Is it running? → the Jobs
API. Not a table, not a count... What is it doing right now? → the app's
in-memory cache, fed by the live stream... What has it been doing? →
Postgres."*

`INFEASIBLE` is the sharpest illustration of why *job status* has to be
harness-authored: a Gurobi task can exit with Databricks
`result_state=SUCCESS` — the container ran fine — while the model concluded
there's no feasible solution, a fact Databricks has no vocabulary for at
all. Only the harness can produce that value.

**Narrowed, not fully closed:** the plan doc left "where does *model*
status live on the wire" explicitly open. It's narrowed now — model status
is tracked in Lakebase, not left to ride `progress.payload` alone — but the
exact shape (a `model_status` column? a separate table?) is still
undecided. Don't read this section as having settled that part.

### What changes

- **The launch path is untouched, because it never gated anything to
  begin with.** `routes/runs.py::trigger_run()` calls `run_now()` directly
  and touches no store at all — there is no slot to stop reserving. If the
  account is genuinely at its concurrency ceiling, Databricks' own
  `queue.enabled` (set on every job resource) queues the excess task; this
  proposal doesn't add or remove anything on that path.
- **The harness becomes the sole writer of *job status* after `QUEUED`.**
  `RUNNING` on startup and the terminal status at the end get written into
  Lakebase's `run_status` directly, alongside the existing unconditional
  write into Delta's `run_events` — not instead of it.
- **The mechanism is the Lakebase Data API, not a Postgres driver.** It's a
  PostgREST-compatible REST interface — every table becomes
  `GET`/`POST`/`PATCH`/`DELETE` endpoints, authenticated with an ordinary
  Databricks OAuth bearer token. The harness already mints exactly that
  token, for the exact same M2M identity, to authenticate its WS connection
  to the app — so this needs **no new dependency and no new credential
  type**, just another `httpx` call. See `docs/v4-rewrite-plan.md`'s
  updated "cost, stated" for the full reasoning; this replaces an earlier
  version of this section that proposed `psycopg`.
- **It has to be best-effort, never blocking the model** — the same
  contract `job/ws.py`'s socket thread already has. A Data API call that
  fails, times out, or comes back non-2xx must not touch what the model
  reports or delay it. `run_events` remains the durable record regardless
  of whether this write lands; it is a better-shaped second mirror of the
  same fact, not a new source of truth.
- **The app stops writing `run_status` entirely.** `services.py::ingest()`
  drops its call to `store.set_status()` on a WS status message — two
  writers of one row was the thing worth removing.
- **Per-run live status still needs no Lakebase read.** `Broadcaster`'s
  `RunSnapshot` (`app/server/broadcaster.py`) already caches the latest
  status/progress per `run_id` in-process precisely so a newly-connecting
  SSE client doesn't need a DB round-trip — untouched, and it becomes the
  *only* live-status path, matching the plan doc's "the app's in-memory
  cache, fed by the live stream."
- **Lakebase reads are for bulk views only** — `list_runs()` in `store.py`
  is the one with a real caller today (`routes/runs.py`'s `GET /api/runs`);
  `non_terminal()` exists but nothing calls it yet, reserved for a
  reconciliation feature that was designed, then removed, and hasn't been
  rebuilt (`app/server/main.py`'s `lifespan` says so directly).
- **`JobsApi.get_run()`/`terminal_status()` answers a different question
  than *job status* entirely** — container liveness, not what the model
  concluded — and stays a fallback of last resort for the one case the
  harness itself can't cover: it died before writing anything anywhere at
  all (e.g. the `sys.exit(0)` failure mode `CLAUDE.md` already documents).

```mermaid
flowchart TB
    RunsRoute["routes/runs.py<br/>POST run-now"]
    Store["PostgresRunStore<br/>set_status() upsert (unchanged)<br/>list_runs() — BULK READS (unchanged)"]
    JobsApiC["JobsApi client"]
    Harness["Harness (job/harness.py)"]
    LakebaseWriter["NEW: best-effort Lakebase Data API writer<br/>RUNNING at start, terminal at end<br/>httpx + the same M2M OAuth bearer token<br/>already used for the WS ingress identity —<br/>no new dependency, no new secret type"]
    RunStatusDb[("run_status (Lakebase)<br/>JOB STATUS: QUEUED/RUNNING/SUCCEEDED/<br/>FAILED/CANCELLED/INFEASIBLE + detail<br/>(model status shape still open)")]
    RunEvents[("run_events (Delta)<br/>unchanged: append-only, unconditional,<br/>still written regardless of the above")]
    RpcClient["RpcClient (job/ws.py)"]
    Broadcaster["Broadcaster: RunSnapshot<br/>unchanged — the live per-run answer,<br/>no DB round-trip"]
    DbxStatus["Container liveness<br/>life_cycle_state / result_state<br/>owned by Databricks, read on demand,<br/>never stored by us"]

    RunsRoute --> JobsApiC
    JobsApiC ==>|"run_now(): no slot claimed<br/>before or after"| Harness
    Harness --> LakebaseWriter
    LakebaseWriter -.->|"best-effort PATCH via the<br/>Lakebase Data API (REST),<br/>never blocks the model"| RunStatusDb
    Harness -->|"unconditional, as today"| RunEvents
    Harness --> RpcClient
    RpcClient <-->|"telemetry + cancel/replay/ping<br/>(unchanged)"| Broadcaster
    JobsApiC -.->|"get_run()/terminal_status():<br/>fallback of last resort,<br/>a different question than job status"| DbxStatus
    Store -.->|"list_runs(): bulk listing reads<br/>(unchanged shape, job-authored rows)"| RunsRoute

    classDef proposed fill:#fff5f5,stroke:#d64545,color:#1a1a1a,stroke-dasharray: 4 2;
    classDef unchanged fill:#f2f2f2,stroke:#888,color:#1a1a1a;
    class LakebaseWriter,RunStatusDb proposed;
    class RunEvents,Broadcaster,DbxStatus,RpcClient,Store,RunsRoute,JobsApiC,Harness unchanged;
```

Red dashed nodes are new; grey ones are today's behaviour, unchanged.
Building this needs: the Lakebase instance's Data API endpoint threaded
through as job config (a name, not a credential — fine as a job parameter
per `CLAUDE.md`'s Secrets rule), and the harness's existing M2M token
acquisition (`job/auth.py::M2MTokenProvider`) reused for one more outbound
call. No new dependency, no new secret type. None of this exists yet;
treat this section as a design note, not a changelog entry.

## Where this stands relative to `CLAUDE.md`

Both diagrams reflect **built** code (`app/server/`, `job/`, `shared/`,
`models/heartbeat`, `models/annealing`) on `v4-plan`, not the eleven-model
target state `CLAUDE.md` describes. The other ten models, the volume→SQL
ingestion job (Slice 4), and automatic startup reconciliation are not drawn
here because they don't exist yet — see `CLAUDE.md`'s "Still not done" list.
The proposed job-authored `run_status` design above is even earlier stage:
it has no code at all yet, on either side — though it isn't a new idea, it's
`docs/v4-rewrite-plan.md`'s own decision record, not yet built.

**Also worth naming: this file's earlier revisions got the ceiling wrong.**
Two prior versions of the diagrams here depicted `PostgresRunStore.claim_slot()`
and `SlotDenied` as live behaviour, and the first proposal drafted here assumed
the app still needed to "claim a slot" at launch. Neither was true — that
machinery is dead code today, and `docs/v4-rewrite-plan.md` had already
retired it in the plan on 2026-08-30. Kept here as a reminder that this file
is drawn from the code and the plan record, not re-derived from first
principles each time.
