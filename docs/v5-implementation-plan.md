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

One item below is now settled; the rest still gate what they always gated.
Item 1 was the reason this phase existed at all, and it's resolved — but
resolving it is what caught the mistake in item 1's own text, so read that
one for the correction as much as the answer.

1. **SETTLED, 2026-09-18.** There is no Lakebase "Data API" in the sense
   earlier drafts of this plan and `docs/architecture-diagram.md` assumed —
   `docs/dbx_external-apps-manual-api.md` only issues a database credential
   over REST; it does not expose tables as CRUD endpoints. The real,
   confirmed mechanism: a normal Postgres connection (`psycopg` or an async
   equivalent), authenticated with an OAuth token as the password, exactly
   what `app/server/store.py`/`app/server/oauth.py` already do for the app.
   Confirmed against a real workspace from a notebook using M2M client
   credentials to read a Lakebase table. See Phase 2 item 2 for the
   credential shape this needs on the job side. `docs/architecture-diagram.md`
   carries the full correction and why the earlier framing was wrong.
2. **One real cancel and one real replay, on the current build, before the
   thread rewrite.** Both are unit-tested but have never been exercised
   against a live run (CLAUDE.md, "Still not done"). Phase 3 rewrites exactly
   the code they live in — get a known-good baseline first, or a regression
   after the rewrite has nothing to be compared against.
3. **Decide what to do about `tests/` before Phase 3 starts, not after.**
   It does not exist on this branch — commit `c1f19c4`, "droped all Test for
   now," removed all of it (`tests/app/`, `tests/job/`, `tests/deploy/`). It
   still exists in full on `origin/main` and `origin/dev`. Meanwhile roughly
   a dozen files still assert specific test files enforce specific contracts
   in the present tense — `CLAUDE.md`, `deploy/README.md`, `models/README.md`,
   `pyproject.toml`, `conftest.py`, `app/server/routes/runs.py`,
   `.claude/agents/transport-app.md`, `docs/message-envelope-spec.md` among
   them. None of those claims are enforced right now. Two honest resolutions,
   not a third: pull a trimmed suite back from `main`/`dev` scoped to what
   this branch actually has (`heartbeat` + `annealing`, not all eleven
   models), or accept the gap and correct every one of those claims to say so.
   Multiple workers changing `job/` and `app/` at once, per the track
   breakdown below, is exactly the situation this gap makes riskiest — no
   fast feedback loop means a collision between two tracks' changes is
   caught by a human reviewer or not at all. Resolve this before, or as,
   Track A below starts.
4. **Check the other branches in this repo before building Phase 2 from
   scratch.** `origin/lakebase-status-history` and
   `origin/claude/durable-writer-and-results` both sound like they already
   cover ground this phase needs — a Lakebase status history and a durable
   writer/results path. Read them first; reconciling with existing work
   costs less than duplicating it.

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
2. **SETTLED, 2026-09-18: a dedicated, separate credential, not the shared
   ingress principal.** Two distinct identities: whatever "normal" OAuth
   client id/secret the job already presents to the app's WS ingress, and a
   separate `lakebase`-specific client id/secret granted its own Postgres
   role on `dbx_leaning.run_status` (`databricks_create_role(...)` in the
   Lakebase SQL editor). Job config needs a second scope/key-name triple,
   distinct from `DBX_OAUTH_SECRET_SCOPE`/`_CLIENT_ID_KEY`/`_SECRET_KEY` —
   e.g. `DBX_LAKEBASE_OAUTH_SECRET_SCOPE` and matching key names — read with
   the same `dbutils.secrets.get` mechanism `job/auth.py::read_secret`
   already uses, not a new one.
3. **Schema migration moves out of both processes.** `store.py::ensure_schema()`
   stops running at app startup; DDL for both Lakebase and Unity Catalog is
   applied out of band, by a human or a separate deploy step, never as a side
   effect of the app or job starting. Rewrite `lakebase_ddl/001_run_status.sql`'s
   own header comment, which currently claims the opposite. `/healthz` reports
   a missing table as degraded rather than the app silently creating one.
4. Harness gains a Lakebase writer, called from `emit()`'s status path,
   queued through the controller thread from Phase 3 rather than blocking
   `emit()` itself — same reasoning as the socket: a slow write must never
   stall the model. This is where item 2's Postgres driver actually gets
   added — `job/requirements.txt`, the harness floor, gains `psycopg` (or
   the async equivalent already used in `app/`), the first Postgres
   dependency the job has ever carried. `job/main.py`'s
   `_build_client`-style pattern (read the second credential once per run,
   build one token provider, close over it) is the template to copy, not a
   new pattern to invent.
5. Remove the app-side status writer (`services.py::_persist_status`) once
   the harness is the sole writer. Two writers to one row is the bug this
   phase exists to prevent.
6. Remove the dead code in `app/server/store.py` — confirmed zero callers
   anywhere outside the file itself, via grep, this session: the `RunStore`
   Protocol in full, `SlotDenied`, `DuplicateRun`, `claim_slot`,
   `release_slot`, `attach_job_run`, `active_count`, `non_terminal`, and the
   `_CEILING_LOCK_ID` constant. Also fix the module docstring, which still
   claims "two implementations behind one interface" and a
   `:class:`WarehouseRunStore`` that does not exist, and drop the
   `Protocol`/`runtime_checkable` imports once nothing uses them. The app
   never enforced the 5-task ceiling; Databricks does, via `queue.enabled` on
   every job resource — confirm that line is already true of every
   `resources/model_*.job.yml`, not just `annealing`'s.
7. Fix `app/server/services.py::_persist_status`'s log message, which
   currently says a failed write will be picked up by "startup
   reconciliation" — that mechanism was removed in the v3→v4 cut (the
   `startup()` method's own comment says so: "What went with it: SqlClient,
   RunRepository, startup reconciliation..."). It is very likely what
   `active_count`/`non_terminal` in item 6 were for; both are now provably
   dead for the same reason. Say plainly that a failed Lakebase write leaves
   `run_events` in Delta as the only record until this write path is retried,
   not that it self-heals on next startup.

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

Phase 0 item 1 (the Lakebase mechanism) no longer gates Phase 2 — it's
settled. Phase 0 items 2 and 3 (the cancel/replay baseline, the `tests/`
decision) still gate Phase 3, since that's the rewrite they exist to protect.
Phases 1, 4 and 5 have no dependency on 0, 2 or 3 and can proceed in parallel
with them. Phase 3 depends on Phase 1's version field existing in `hello`
before the controller's dispatch table is written, so the two are best done
together rather than strictly sequentially.

## Running this across multiple workers

This branch (`v4-plan`) is design and cleanup only — no phase above has been
implemented. The next step is a new branch, cut from this one once it's
pushed, where the phases above get built. This section is how to split that
work across more than one worker without them colliding.

### Before fanning out

1. Resolve Phase 0 item 3 (`tests/`) first. Every track below is easier to
   verify, and easier to review, with a real test suite to run — and
   multiple workers touching `job/` and `app/` at once is the situation
   where "no fast feedback loop" costs the most.
2. Read `origin/lakebase-status-history` and
   `origin/claude/durable-writer-and-results` (Phase 0 item 4) before Track C
   below starts, so it builds on or reconciles with what's there instead of
   duplicating it.
3. Cut the new branch from this one's current tip, after the doc/cleanup
   commits in this session are pushed.

### Track breakdown

| Track | Phase(s) | Files it owns | Depends on |
|---|---|---|---|
| A — cleanup carried over from this session | Phase 2 items 6–7 | `app/server/store.py`, `app/server/services.py` | Nothing. Safe to start immediately; it's dead-code removal and a log-message fix, already scoped exactly by grep in this session. |
| B — wire freeze | Phase 1 | `app/shared/envelope.py`, `shared/rpc.py`, `app/server/routes/rpc.py`, `docs/message-envelope-spec.md` | Nothing. |
| C — run state | Phase 2 items 1, 3–7 | `lakebase_ddl/001_run_status.sql`, `app/server/store.py` (schema/DDL parts, not Track A's dead code), `job/requirements.txt`, a new job-side Lakebase writer module, the second credential's job parameters | Phase 0 item 4 (read the sibling branches first). Item 1 is settled, no longer a gate. Needs Track A's docstring fix landed first if both touch `store.py`'s header — coordinate or sequence, don't run fully blind in parallel on the same file. |
| D — harness concurrency | Phase 3 | `job/harness.py`, `job/ws.py`, `job/main.py` | Track B's version field (item 3) should land first — the controller's dispatch table is easier to write once, not twice. |
| E — discovery & cross-team | Phase 4 | `app/server/discovery.py`, `resources/*.job.yml` | Nothing. |
| F — docs & packaging | Phase 5 | `docs/model-expansion-and-packaging.md`, `docs/architecture-diagram.md` | Best started last — it documents what B–E actually did, not what this plan predicted they'd do. |

**The one real file collision: Track C and Track D both change `job/harness.py`.**
Phase 2 item 4 calls for the Lakebase writer to be invoked from `emit()`'s
status path, queued through the controller thread Phase 3 item 5 creates.
That is not two disjoint changes to the same file, it is one change that
happens to be described in two phases. Either run C and D as one worker, or
sequence them: D lands the named-slot restructure first (item 5), then C
plugs a writer into the slot it creates. Don't run them as two independent
workers against `job/harness.py` at the same time.

### Running it

Same two options CLAUDE.md's now-deleted `parallelization-plan.md` used, and
for the same reasons:

- **One worktree and branch per track**, each its own Claude Code session —
  true parallelism, no file contention as long as the table above is
  respected.
- **One orchestrating session**, dispatching each track as a subagent — fine
  for tracks B, E and F, which touch disjoint files; use worktrees for A/C/D
  once C and D are actually running concurrently with anything else, since
  that's the one place a shared-file collision is real rather than
  theoretical.

Merge order follows the phase dependencies above: B and E merge whenever
ready; A anytime, ideally first since nothing depends on it; C and D merge
together (or D then C); F last, once it has something true to document.
