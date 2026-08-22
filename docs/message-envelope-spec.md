# Message envelope — spec only, no code yet

This is a contract, not an implementation. The first session to touch
`shared/` implements this as real (Pydantic) code; every other track —
models, job, app, frontend — builds against this document until then.

**Do not let a model-building track invent its own message shape.** If this
spec is ambiguous or missing something a model needs, that is a reason to
update this file (and flag it), not to improvise locally — a shape invented
inside `models/gurobi_scheduling/` and copied nowhere else is exactly how v1
ended up with drift between what the socket sent and what the table stored.

## Why one envelope

Every record a run produces — a log line, a progress sample, a status
transition, a result — is the same kind of thing: *something that happened
during a run, at a point in time, that something downstream wants to see*.
Giving them one shape means the transport (WebSocket, HTTP push, Delta) never
needs to know what it's carrying, and the client has exactly one parser.

## Common envelope fields (every message has these)

| Field | Type | Notes |
|---|---|---|
| `type` | string enum | `"log"` \| `"progress"` \| `"status"` \| `"result"` — the discriminator |
| `run_id` | string | Which run this belongs to |
| `seq` | integer | **Assigned by the job.** One monotonic counter per run, shared across all message types — not per-type. This is what lets a client dedupe live-vs-backfilled records with a single cursor. Never assigned by a UC identity column: the live channel and the durable table are independent, and only a value known before the durable write can reconcile them. |
| `ts` | integer (epoch ms) | Not a formatted timestamp — avoids timezone ambiguity and parsing on both ends. Epoch ms (not seconds): solver log lines can be sub-millisecond apart and epoch seconds would collide. |

`seq` must be **monotonic and free of gaps by construction** for a given run.
If a client observes a gap, that gap must mean "these records exist and
haven't arrived yet" — never "the job skipped some seq values on purpose."
Gaps that are actually normal (e.g. a filtered-out debug line) still consume
a seq value; they are not renumbered around.

**A gap on the live path is answered by backfilling, not by waiting.** Logs
are droppable on the live path and `client_visible=false` records are never
sent live at all, so a live gap is *routine* and the missing records may never
arrive over that channel. They are always in Delta. So the client's rule is
"gap → fetch from the durable store when you actually need it", not "gap →
block until it turns up". The durable record is the one that is gap-free in
the strong sense; `tests/integration/test_end_to_end.py` asserts that.

## `log`

Best-effort, for progress display and debugging — not a result, and not
required to be lossless on the live path.

| Field | Type | Notes |
|---|---|---|
| `message` | string | |
| `level` | enum | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` |
| `source` | string | e.g. `"gurobi"`, `"model"`, `"job"` — free-ish per model family, but keep it small and consistent within a family |
| `phase` | string | e.g. `"input"`, `"build"`, `"solve"`, `"results"` |
| `client_visible` | boolean | `false` = retained in durable storage but not sent to the browser live (e.g. raw solver chatter kept for offline tooling, not shown in the UI) |

The durable path stores everything regardless of `client_visible` — that
field filters the **live** send, not what gets written.

## `progress`

One sampled point on whatever "how is this run doing" curve applies. Sampled
— not every solver iteration, not every training step. A model chooses its
own sampling cadence but should not flood this (guideline: a few times a
second, at most, and typically every few seconds for long runs).

| Field | Type | Notes |
|---|---|---|
| `elapsed_seconds` | float | Time since the run started solving/training/sampling |
| `percent_complete` | float 0–100, nullable | Not always knowable (e.g. MIP progress isn't a percentage) — null when it isn't |
| `primary_metric` | float, nullable | The one number this model's progress view leads with (MIP gap, validation loss, R-hat, whatever fits) |
| `primary_metric_label` | string, nullable | What `primary_metric` means, e.g. `"mip_gap"`, `"val_loss"`, `"max_rhat"` — the frontend uses this to label the chart, not to branch logic on |
| `payload` | object (free-form) | Model-specific extra fields the generic progress view doesn't need but a model-specific view might (e.g. Gurobi's `best_bound`/`incumbent`/`nodes_explored`, or MCMC's `divergences`) |

The generic fields (`percent_complete`, `primary_metric`) let *any* model
render a minimally useful progress view with zero model-specific frontend
code. `payload` is where a model earns a richer, model-specific view later
without changing the envelope.

Known sentinel to handle explicitly per model: Gurobi reports `±1e100` for
the incumbent before the first solution is found — store that as `null`,
never raw, or it poisons a chart's axis.

## `status`

A lifecycle transition. Not backed by its own table — `run_status` is a
row that gets `UPDATE`d, and the live status message is a notification of
that update, not the record of truth.

| Field | Type | Notes |
|---|---|---|
| `status` | enum | `QUEUED` \| `RUNNING` \| `SUCCEEDED` \| `FAILED` \| `CANCELLED` \| `INFEASIBLE` (extend per model family only if genuinely needed — prefer reusing these) |
| `detail` | string, optional | Free text, e.g. `"run complete"`, an error summary |

## `result`

**Not best-effort.** Written whenever the model's code reaches the point of
having results, regardless of what the terminal status ends up being — a
cancelled run keeps whatever it had. This is a deliberate difference from
`log`: results must not silently disappear because a live channel dropped
them, and a run must never claim success while actually having produced
nothing.

| Field | Type | Notes |
|---|---|---|
| `preview` | array of objects, bounded (~500–1000 points) | A **downsampled** preview, not the full result set — enough to render "the pretty graph" (convergence curve, forecast series, trace plot) instantly. Use LTTB (Largest-Triangle-Three-Buckets) for downsampling time-series-shaped results, not naive stride sampling — stride sampling hides spikes exactly where they matter (e.g. a forecast error blow-up) |
| `row_count` | integer | Total rows actually written to the durable results table — **this is the field that lets "succeeded, wrote 8,760 rows" be distinguished from "succeeded, wrote 0 rows because the write failed."** Always populate it, even when 0 |
| `fetch_hint` | object | Enough for the client to pull the full result set on demand (table name, run_id, however the per-model results table is keyed) — not the results themselves |
| `chunk_index` | integer, default 0 | Which chunk of a multi-emission run this is. **Distinct from `seq`**, which counts every message of every type: two result chunks may be chunk 0 and 1 while being seq 40 and 91. 0 for the common once-at-the-end case |
| `final` | boolean, default true | False while more chunks are still coming. A run's results are complete once a message with `final=true` has been seen |

### Incremental results (added — see the changelog)

A model that produces results in chunks (a rolling-origin backtest, chunked
batch inference) emits one `result` message **per chunk**, each with its own
`chunk_index` and its own `row_count` — that chunk's count, never a running
total. `models/streaming_results/` is the model that exercises this, and its
tests fail loudly if the harness stops supporting it.

The rows themselves never travel on the message. A model calls
`emit("result", rows=[...])` and the harness writes them to the model's
results table, counts what it wrote into `row_count`, and builds the bounded
`preview`. See `models/README.md`.

Per-model result **tables** are separate from this envelope — each model
family has its own results schema in Unity Catalog, governed by its own UC
grants, because different models serve different audiences. The `result`
*message* is only ever a summary/pointer/preview; the full data lives in
that model's own table and is read directly, not replayed through this
envelope.

## Encoding (not part of the contract — this is a delivery detail)

- msgpack: job → app, and in the Delta write buffer.
- JSON: app → browser (SSE). Native to the browser, readable in devtools,
  already compressed by the transport.
- The envelope's job is to define valid *shape*. Whatever encodes it
  (msgpack, JSON, whatever comes next) must produce byte-identical logical
  content — that interchangeability is the test that the boundary between
  "protocol" and "serialisation" is drawn in the right place.
- Validation (e.g. Pydantic models once this is implemented) lives with
  whichever side is deserialising, not inside the encoding step itself.

## What a model actually sees

A model never imports this spec, msgpack, WebSockets, or anything
transport-related. It is handed a plain callback (something like
`emit(type: str, **fields)`) by the job harness and calls it with
envelope-shaped keyword arguments; the harness is responsible for stamping
`run_id`/`seq`/`ts` and getting the message onto every active channel. See
the relevant `.claude/agents/model-*.md` file for what a model's actual
Python surface looks like.


## Changelog

Amendments to this contract, so a track that built against an earlier reading
can see what moved. The rule from the top of this file still holds: if the
spec is ambiguous or missing something a model needs, amend it here and flag
it — do not improvise locally.

### 2026-08-22 — `result.chunk_index` and `result.final`

Added while implementing `shared/`. `models/streaming_results/` needs to emit
results repeatedly during one run, and the spec had no way to say which chunk
a message was, or whether more were coming. `seq` cannot serve: it counts
every message of every type, so consecutive chunks are not consecutive seqs.
Both fields default to the once-at-the-end case (`0`, `true`), so no existing
reading of the contract changes.

### 2026-08-22 — live gaps are backfilled, not waited on

Clarification, not a change. "Gap means these records exist and haven't
arrived yet" was true but incomplete: on the live path they may never arrive,
because logs are droppable there by contract. Spelled out under the common
fields.
