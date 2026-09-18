# v5 — implementation plan

Written 2026-09-18, from the design session that followed `docs/v4-rewrite-plan.md`
and the diagrams in `docs/architecture-diagram.md`. v4 settled the transport
shape — WS live, Delta durable, one envelope. v5 is not a new transport. It is
four things v4 left open once a real workspace and a real second consumer
started to press on it: **who writes run state, how the job survives its own
concurrency, what the wire promises a fork, and what a schema migration is
allowed to be.**

**Nothing here is a rewrite of what works.** Slice 1 (heartbeat, end to end,
confirmed 2026-09-04) is untouched. This plan only touches: the job's thread
model, the run-status write path, the envelope's tolerance rules, and three
small doc/packaging gaps. Everything not listed in a phase below is out of
scope for v5.

## Status of this plan

Design-only until this document lands. No code in this session implemented
any phase below except the one exception already merged: `job/main.py`'s
SIGTERM/SIGINT chaining (`09e2c80`), which the user explicitly carved out of
the "design only" rule because it was small and already agreed.

A few items are marked **PROPOSED** below rather than **SETTLED**. Those are
mine, offered during the design session and not yet explicitly signed off —
implementing them without confirming the shape first would be guessing on
someone else's behalf.

## Phase 0 — validate before building on it

Two things in this plan rest on facts nobody has confirmed against a real
workspace. Spike both before Phase 2 writes code against them; a design built
on an unverified REST endpoint is the same mistake the ingress spikes existed
to avoid.

1. **The Lakebase Data API, reached from a serverless job task.**
   Confirm: the endpoint is reachable through the trusted-domain egress list;
   the existing `M2MTokenProvider` token is accepted by the Data API in the
   same `Authorization: Bearer` shape it already uses against Postgres; and
   which service principal needs which grant (see Phase 2, item 2).
2. **One real cancel and one real replay, on the current build, before the
   thread rewrite.** Both are unit-tested but have never been exercised
   against a live run (CLAUDE.md, "Still not done"). Phase 3 rewrites exactly
   the code they live in — get a known-good baseline first, or a regression
   after the rewrite has nothing to be compared against.

## Phase 1 — freeze the wire

The wire, not the harness, is the compatibility promise a fork owes the app.
Do this before Phase 3 makes the harness itself easy to fork.

1. Envelope: `extra="forbid"` → `extra="ignore"`.
2. An unrecognised `type` is logged and skipped at the parse boundary
   (`app/server/routes/rpc.py::_invoke`, the `MessageAdapter.validate_python`
   call), never fatal to the socket loop.
3. A mandatory version field on `hello`'s params. **PROPOSED** compatibility
   rule: the app accepts any job whose major version matches and whose minor
   is less than or equal to the app's own. Write the exact rule down, not
   just the field — an unenforced version number is decoration.
4. Formalise the existing custom RPC to the JSON-RPC 2.0 shape, with LSP's
   capability-exchange convention as the model for `hello`.
5. Write down the part-file layout (directory shape, file naming, the
   msgpack framing, one-terminal-status-per-run) as part of the frozen
   contract in `docs/message-envelope-spec.md`. It is read by replay today
   and will be read by the Slice 4 ingestion job later; nothing has written
   its shape down as a promise yet.
6. Changelog entry in `docs/message-envelope-spec.md` for items 1–4.

**Explicitly deferred out of v5**, into the same later bucket as msgpack and
Arrow IPC on the wire:
- A `register`/capabilities RPC notification after `hello`. Real idea, no
  concrete registrant yet — building the slot before anything fills it is
  the speculative-build CLAUDE.md already warns against.
- Any serialisation swap: protobuf, Cap'n Proto, FlatBuffers. Rejected for
  this project's actual throughput regime and for recoupling shape to wire
  format, which the current split (Pydantic outside msgpack/JSON) exists to
  avoid. See the design session's reasoning if this comes up again.

## Phase 2 — run state moves to where it's authoritative

1. **`run_status` column shape — PROPOSED, needs sign-off before the DDL is
   written:**
   `run_id` (PK), `status` (free text, not an enum — the vocabulary is the
   model's), `terminal` (bool), `detail` (text), `updated_at`, `seq` at time
   of write. Lakebase holds only custom/nuanced status (e.g. `INFEASIBLE`);
   `QUEUED`/`RUNNING`/plain `SUCCEEDED`/`FAILED`/`CANCELLED` stay in the
   Jobs API and are never mirrored here.
2. **Identity and grant for the harness's Lakebase write — decide, don't
   assume.** Either the existing shared ingress principal gets a second
   grant (a Postgres role on `dbx_leaning.run_status`, on top of `CAN_USE` on
   the app), or a dedicated third credential is provisioned, mirroring
   `DBX_OAUTH_SECRET_SCOPE`/`_CLIENT_ID_KEY`/`_SECRET_KEY`. Pick one before
   Phase 0's spike, since the spike needs a real credential to test against.
3. **Schema migration moves out of both processes.** `store.py::ensure_schema()`
   stops running at app startup; DDL for both Lakebase and Unity Catalog is
   applied out of band, by a human or a separate deploy step, never as a side
   effect of the app or job starting. Rewrite `lakebase_ddl/001_run_status.sql`'s
   own header comment, which currently claims the opposite. `/healthz` reports
   a missing table as degraded rather than the app silently creating one.
4. Harness gains a Lakebase writer, called from `emit()`'s status path,
   queued through the controller thread from Phase 3 rather than blocking
   `emit()` itself — same reasoning as the socket: a slow write must never
   stall the model.
5. Remove the app-side status writer (`services.py::_persist_status`) once
   the harness is the sole writer. Two writers to one row is the bug this
   phase exists to prevent.
6. Remove the dead code in `app/server/store.py`: `claim_slot`, `SlotDenied`,
   `DuplicateRun`, the ceiling advisory-lock id, and the stale docstring
   mentioning `WarehouseRunStore`. The app never enforced the 5-task ceiling;
   Databricks does, via `queue.enabled` on every job resource — confirm that
   line is already true of every `resources/model_*.job.yml`, not just
   `annealing`'s.

## Phase 3 — the harness becomes a composable object

1. Four threads: main (model), controller, sender, receiver — replacing the
   three-thread doc (main/roller/socket) and its "two places only" shared-state
   undercount. The roller's flush work moves under the controller. All RPC
   dispatch, present and future, runs on the controller, never on main.
2. Sender: blocking queue reads replace the 10ms polling loop in
   `job/ws.py::RpcClient._loop`. `emit()` never calls the socket directly —
   `sendall` can block indefinitely with no timeout outside the closing
   handshake, which is exactly the stall a model-blocking main thread cannot
   afford to inherit.
3. **PROPOSED** drop policy: two logical queues, or a type check at the drop
   point, so a full queue drops `log` records (best-effort, per the envelope
   spec) and never `status` or `result` (never best-effort, per the same
   spec). Today's single queue drops oldest regardless of type.
4. **PROPOSED** terminal shutdown sequence, written down before the shutdown
   code is touched: model's own result write → part files flushed → Lakebase
   status write → `bye`. A crash between any two must leave Delta as the
   floor and Lakebase as at-most-stale, never the reverse.
5. Restructure `Harness` so its swappable parts — writer, channel, cancel
   token, sequence counter, model handle — are named slots on the object,
   replacing the private-attribute assignment in `job/main.py`
   (`harness._on_message = client.send  # noqa: SLF001`). This is what makes
   "modify the harness freely, as long as the wire contract holds" (the
   persona-c decision from this session) an actual supported path rather
   than something only a full fork can do cleanly.
6. No multiprocessing, no async rewrite. Both considered and rejected in the
   design session — a model's own GIL-holding C extension is not fixed by
   more threads, and async only pays for itself with an event loop of its
   own, which is not worth it at this scale.

## Phase 4 — discovery and cross-team readiness

1. Periodic refresh of the two discovery tags (`app/server/discovery.py`),
   replacing the startup-only `_resolve_job_ids()`.
2. Make `discovery.PROJECT_TAG` configurable per deployment, so a team
   running their own instance of the app can point it at only their own jobs
   without forking `discovery.py`.
3. **PROPOSED** task-scoped run id convention for chained multi-task jobs —
   today a job parameter is job-level, so every task in a chain gets the
   same `DBX_RUN_ID` and would collide on `runs/<run_id>/` in the telemetry
   volume and on the Lakebase primary key. Exact format (job run id + task
   name, vs. the task's own run id) still open.

## Phase 5 — docs and packaging

1. Write `docs/model-expansion-and-packaging.md`. CLAUDE.md's docs index has
   listed it since before this session; it does not exist on `v4-plan`.
   Content: the three exported surfaces (modelkit for the data-science
   persona, harness + envelope for another engineering team, the job-resource
   template + configurable tag for app reuse), what each promises, and the
   trigger-sequenced order for when each surface actually needs to become
   installable outside this repo (a wheel on a UC volume, when a first
   external consumer exists — not before).
2. Update `docs/architecture-diagram.md`'s "Proposed: job-authored run_status"
   section to the narrowed rule from Phase 2, item 1 — it currently predates
   the "custom status only, nuanced terminal outcomes only" refinement.
3. `CLAUDE.md` docs index: confirm the v5 doc is listed once this lands.

## Rejected, for the record

So a future session doesn't re-open these without new information:

- **gRPC / Thrift / Cap'n Proto RPC** — ingress survival unverified (same
  open question the WS/SSE spikes closed for this project's actual
  transport), and each pulls its own weight into every model environment via
  the shared harness.
- **Protobuf / Cap'n Proto / FlatBuffers as the envelope's wire format** —
  none close the actual gap (an app that doesn't statically know a model's
  status vocabulary); that gap closes by keeping the app's required fields
  minimal and treating model-specific values as opaque, not by a serialiser
  swap. All three also reopen the shape-vs-serialiser coupling the current
  split (Pydantic outside msgpack/JSON) was built to avoid.
- **An MQTT broker as a second app** — raw TCP survival through the Apps
  ingress is exactly as unverified as gRPC's, and it duplicates the 24h
  lifecycle problem the existing app already has.
- **Multiprocessing / process isolation for model safety** — considered for
  the concurrency rewrite, not worth it at this scale; see Phase 3, item 6.
- **The app's own 5-concurrent-task gate** — already removed from the design
  in this session (`dbc0b72`); Databricks enforces the real ceiling via
  `queue.enabled`.
- **Schema migration frameworks (Alembic or equivalent) in app or job** —
  schema management stays fully out of band; see Phase 2, item 3.

## Sequencing

Phase 0 gates Phase 2 (the Lakebase write needs a confirmed credential and a
confirmed reachable endpoint) and should happen before Phase 3 touches the
socket code the baseline cancel/replay run exercises. Phases 1, 4 and 5 have
no dependency on 0, 2 or 3 and can proceed in parallel with them. Phase 3
depends on Phase 1's version field existing in `hello` before the
controller's dispatch table is written, so the two are best done together
rather than strictly sequentially.
