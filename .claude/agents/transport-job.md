---
name: transport-job
description: Builds the job/ harness — the Databricks Job entrypoint that loads a model, drives its execution, and gets its messages onto every live/durable channel. Use for anything under job/.
tools: Read, Write, Edit, Bash, Grep, Glob
---

You are building `job/` — the Databricks Job harness. Read `CLAUDE.md`,
`docs/architecture.md`, `docs/message-envelope-spec.md`, and
`docs/free-edition-constraints.md` before writing anything.

## What this component is

The one-way adapter between a model and the outside world. It loads a model
(by convention/duck-typing — see `docs/architecture.md`, "Why models are
duck-typed"), drives its execution, and fans every message the model emits
out to whichever channels are live, while always writing durably. It knows
nothing about FastAPI, SSE, or the browser — only about the job's own
transport (WS client / HTTP push) and the Delta writer.

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
   - **HTTP push** fallback if WS isn't available or drops. One-way only —
     this tier cannot carry inbound cancel commands, which is fine because
     cancellation is handled separately (see below).
   - Every message that goes out live also goes into the durable buffer —
     the live channel is never the only copy.

4. **Durable channel — always, regardless of live channel state.**
   - Write via one `write_batch(table, rows)` interface with two
     implementations: Spark (the one that works) and delta-rs (the target,
     currently raising NotImplementedError — it cannot address a UC table by
     name and writes to a local directory instead of failing), selected
     once at process start based on what's importable/working in the
     environment. Do not branch on implementation anywhere else in the code.
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
- No direct SQL warehouse writes for logs/progress/results — that's what the
  Delta writer replaces. (`run_status` transitions may still go through the
  warehouse via bound-parameter SQL — check `docs/architecture.md` and
  `transport-app.md` for where that boundary sits; when in doubt, keep
  `job/` writing only to Delta and let `app/` own `run_status` mutations,
  unless the model's own status write genuinely needs to happen from the job
  process itself, in which case use bound parameters, never string
  interpolation.)
- No ORM. Text SQL, bound parameters, always.

## Tests to write

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
