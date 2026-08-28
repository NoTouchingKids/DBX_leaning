---
name: transport-job
description: Design record for job/, the harness (BUILT) — the Databricks Job entrypoint that loads a model, drives its execution, and gets its messages onto every live/durable channel. Use for anything under job/.
tools: Read, Write, Edit, Bash, Grep, Glob
---

You are working on `job/` — the Databricks Job harness. It is **built and
tested**; this brief is the design record, not a to-do list, and it has held
up better than the others — read it as still-current unless the source says
otherwise. Read `CLAUDE.md`,
`docs/architecture.md`, `docs/message-envelope-spec.md`, and
`docs/free-edition-constraints.md` before writing anything.

## What this component is

The one-way adapter between a model and the outside world. It loads a model
(by convention/duck-typing — see `docs/architecture.md`, "Why models are
duck-typed"), drives its execution, and fans every message the model emits
out over the WebSocket when one is up, while always writing durably. It knows
nothing about FastAPI, SSE, or the browser — only about the job's own
transport (`job/bus.py`) and the Delta writer.

Nothing here is model-specific. If you find yourself writing an `if
model_type ==` branch, that logic belongs in the model, not here.

## The hazard you must design around from the start

`model.optimize()` / `model.fit()` / whatever the model's blocking call is
runs synchronously and can take a long time. To keep a WebSocket connection
alive concurrently, that call has to run off the main async loop — in a
thread executor. **This means any callback the model invokes from inside
that call fires on a worker thread, not the event loop.**

Consequence: you cannot safely call `asyncio.Queue.put_nowait()` from that
callback. `asyncio.Queue` is not thread-safe. The correct crossing is one of:

- `loop.call_soon_threadsafe(queue.put_nowait, msg)` — stdlib, no dependency,
  preferred
- a plain `queue.Queue` (thread-safe by design) written by the model's
  thread, drained by one dedicated async task via `asyncio.to_thread` or a
  polling `get_nowait()` loop

Do **not** reach for a third-party sync/async queue library (e.g. culsans)
for this — it's alpha software by its own documentation, single-maintainer,
and the stdlib crossing above is three lines. Only reconsider if the stdlib
approach demonstrably doesn't fit once real code exists.

**Termination is a `threading.Event`, not an `asyncio.Event`.** The model's
callback (running on the worker thread) polls `.is_set()`; the event loop
sets it in response to a cancel command arriving over WS or HTTP.

Get this one boundary right — it's most of what makes this component correct
or subtly broken.

## Responsibilities

1. **Load the model.** Duck-typed discovery per `docs/architecture.md` — a
   configurable import path, then look for conventional attribute/method
   names for build/run/results/cancel-check. Fail with a readable message
   listing exactly what was tried if something required is missing.

2. **Drive execution**, handing the model an `emit(type, **fields)` callback
   that:
   - stamps `run_id`, the next `seq` (one monotonic counter, shared across
     all message types for this run — see the envelope spec), and `ts`
   - fans the resulting envelope message out to every active channel

3. **Live channel, in order of preference:**
   - **WebSocket** to the app, at `run_id`-scoped connection. Reconnect
     periodically (~30–60s) if not currently connected — a run started
     while the app is down should attach automatically once the app comes
     up, with no restart needed.
   - **There is no second live channel.** The HTTP push fallback used to be
     one and was removed: it could not carry a cancel, could not answer a
     backfill, and existed for a socket that has since been proven through
     the Databricks Apps ingress. The app's `/api/runs/{id}/push` endpoint
     still exists; the job simply does not use it. Do not reintroduce a
     channel abstraction to hold one implementation.
   - Every message that goes out live also goes into the durable buffer AND
     into the job's replay ring (`job/record.py`) — the live path is never
     the only copy, which is what makes its drop policy allowed to be
     "oldest first, no tiers": anything dropped is recoverable by BACKFILL.
   - **Drain before closing at teardown.** `WebSocketBus.drain()` first, then
     `close()`. The other order cost a fast run its entire live stream,
     terminal status included, because the send queue drained into an
     already-shut socket. `drain()` also hoists status/result to the front,
     so a socket too slow to clear the backlog still delivers the outcome.

4. **Durable channel — always, regardless of live channel state.**
   - Write via one `write_batch(table, rows)` interface, chosen once at
     process start and never branched on again. `DBX_WRITER` takes three
     values (`job/delta.py`, `WriterKind`):
     **`spark`** is the only real path and what `auto` resolves to;
     **`jsonl`** is a local development writer, and `auto` only falls back to
     it when `DBX_ALLOW_LOCAL_WRITER=1` is set explicitly — otherwise no
     Spark session is a hard `RuntimeError`, because silently writing a
     production run's telemetry to a discarded file is worse than failing.
   - Flush on **whichever comes first**: buffered size ≥ 1 MB (configurable),
     age since last flush ≥ 30s (configurable), or end of run. The age bound
     exists specifically to cap how much is lost if the process dies —
     don't let size alone gate a flush.
   - Buffer per destination table (`run_logs`, `run_progress`, per-model
     results tables) independently; flushing one should not block another.
   - Encode with msgpack for the buffer; the envelope spec's shape is what's
     being preserved, not the encoding.

5. **Results — write when the model produces them, not only on success.**
   A cancelled run keeps whatever partial results the model already
   produced. Populate `result.row_count` from what was actually written,
   including 0 — never let a status of `SUCCEEDED` reach `run_status` if the
   result write itself failed; that combination must be structurally
   impossible; use a status that reflects the write failure instead.

6. **Cancellation — inbound over WS only.**
   - A cancel command arriving over the WS sets the `threading.Event`.
   - There is no durable/warehouse-polling fallback for cancel *from this
     side* — cancel is initiated by the app (see `transport-app.md`), and if
     no live channel exists at all, the operational escape hatch is
     `databricks jobs cancel-run` (a hard kill, outside this harness's
     control — document this, don't try to build around it).

7. **Degrade cleanly with nothing connected.** No `APP_URL`, unreachable app,
   or WS failure must all result in: the run proceeds, durable writes still
   happen, nothing raises. This is the single most important property to
   have a test for — a job that starts while the app is down is a normal
   case here, not an edge case.

## Explicit non-goals

- No model-specific logic of any kind.
- **No SQL of any kind, to any store.** That boundary was left open when this
  brief was written ("`run_status` transitions may still go through the
  warehouse..."); it has since closed and `job/` contains no SQL at all. The
  job writes append-only telemetry to Delta through `write_batch`, and
  `app/` owns every `run_status` mutation — which now lives in Lakebase
  Postgres, not the warehouse, and is not reachable from a job task anyway.
  The job's contribution to run state is the `status` messages it emits,
  which land in `run_events` and are what startup reconciliation reads back.
- No ORM, and nothing that needs one.

## Tests

Written, under `tests/job/`. The list is kept because it is still what must
not regress:

- Model loader: successful discovery, and the specific failure message when
  a required piece is missing.
- The threading→async queue crossing: a message emitted from a background
  thread reliably reaches the async consumer, under load (many messages in
  a burst), without loss or corruption.
- Termination: setting the `threading.Event` from the async side is observed
  by the (simulated) blocking call within one poll interval.
- Flush triggers: size threshold, age threshold, and end-of-run each
  independently trigger a flush; whichever fires first wins.
- Full degradation: no APP_URL set at all → run completes, durable writes
  happen, nothing raises.
- Results written regardless of terminal status, including on cancellation.
