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
            Store["PostgresRunStore<br/>claim_slot / set_status / non_terminal"]
            JobsApiC["JobsApi client<br/>run_now / get_run / terminal_status()"]
            OAuthC["OAuthTokenProvider<br/>M2M client-credentials"]
        end
    end

    subgraph JobTask["Databricks Job task — one serverless task per run<br/>max 5 concurrent, account-wide"]
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
    RunsRoute --> Store
    RunsRoute --> JobsApiC
    RpcRoute --> JobConn
    JobConn --> Broadcaster
    Broadcaster -->|"fan out to every SSE<br/>subscriber of this run_id"| StreamRoute
    RpcRoute -.->|"ingest(): mirrors the latest status into run_status —<br/>a best-effort CACHE for fast point-lookups &amp; the<br/>concurrency ceiling. NOT authoritative: run_events is"| Store

    %% ---- app -> job, via the Jobs REST API (not drawn as its own system —<br/>       it's a plain Databricks API call, same as any other) ----
    JobsApiC ==>|"run_now(job_id, job_parameters) via Jobs API<br/>-&gt; starts a new serverless task<br/>DATABRICKS_HOST as a task parameter"| Harness
    JobsApiC -.->|"get_run() -&gt; terminal_status() via Jobs API<br/>on demand, not a poll loop"| Harness
    OAuthC <-->|"client-credentials"| OIDC
    JobsApiC -.-> OAuthC

    %% ---- app <-> lakebase ----
    Store <-->|"parameterised SQL, bound params<br/>claim_slot: advisory-lock txn = real 5-task ceiling"| RunStatus

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
  the app needs live — a point lookup by `run_id` and an atomic
  count-and-claim against the 5-task ceiling — so the app keeps its own
  one-row-per-run mirror, best-effort, updated when a WS status message
  happens to arrive. The job has no Postgres/Lakebase code at all
  (`app/shared/tables.py` says this outright); it never writes that mirror.
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
    A->>P: claim_slot() — advisory-lock txn:<br/>count active vs ceiling(5), insert QUEUED
    alt ceiling already taken
        P-->>A: SlotDenied
        A-->>U: 409, wait for a slot
    else slot claimed
        A->>J: run_now(job_id, job_parameters) via Jobs API<br/>starts a new serverless task
        A->>P: attach_job_run(job_run_id)
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
            A->>P: set_status(run_id, terminal_status)<br/>— refresh the app's cache, nothing more
            A-->>U: SSE: status terminal
        end
    end
    Note over A,J: If the WS was never up, or dropped and stayed down,<br/>none of the opt-WS-is-connected steps happen — P's cache goes stale.<br/>The run still finishes and D still has the true terminal status either way,<br/>the job never depended on the app to make its own status authoritative.<br/>JobsApi.get_run()/terminal_status() can also answer did-it-finish<br/>on demand from the Jobs API, which cannot go stale by construction.
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
- The terminal `set_status()` call into `P` (Lakebase) is labelled as a cache
  refresh, not a source of truth — because it isn't one. The job's own write
  into `D`'s `run_events`, one step earlier, is what makes the terminal status
  real; it happens whether or not the app, the WS, or Lakebase are anywhere
  in the picture.
- The final note is deliberately hedged: `JobsApi.get_run()` and
  `terminal_status()` exist and can answer "did this finish?" for a
  `job_run_id` on demand, but as of this writing `app/server/main.py`'s
  `lifespan` does **not** call them automatically at startup to reconcile
  stale `run_status` rows — an earlier warehouse-based reconciliation step was
  removed and nothing has replaced it yet. Don't read this diagram as saying
  otherwise; check `app/server/main.py` and `app/server/services.py` before
  relying on automatic reconciliation existing.

## Where this stands relative to `CLAUDE.md`

Both diagrams reflect **built** code (`app/server/`, `job/`, `shared/`,
`models/heartbeat`, `models/annealing`) on `v4-plan`, not the eleven-model
target state `CLAUDE.md` describes. The other ten models, the volume→SQL
ingestion job (Slice 4), and automatic startup reconciliation are not drawn
here because they don't exist yet — see `CLAUDE.md`'s "Still not done" list.
