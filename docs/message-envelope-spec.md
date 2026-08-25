# Message envelope — the wire contract

This is the contract in prose. It is implemented, in Pydantic, in
`shared/envelope.py`, and published as JSON Schema under `schema/` — so if
this file and `shared/envelope.py` ever disagree, the code is right and this
file is stale; say so and fix it here. Every track — models, job, app,
frontend — builds against this shape.

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

Known sentinel: Gurobi reports `±1e100` for the incumbent and bound before
the first solution is found, and that must reach the envelope as `null`,
never raw, or it poisons a chart's axis. A Gurobi model does not have to
remember this — `job/drivers/gurobi.py` holds it as `GUROBI_SENTINEL` and
nulls it on the way out. It is called out here because the value is *finite*,
so `shared.envelope.sanitize_metric` (which only catches inf/NaN) cannot see
it: anything else emitting a magic large number has to null it itself.

## `status`

A lifecycle transition. Not backed by its own table — `run_status` is a
row that gets `UPDATE`d, and the live status message is a notification of
that update, not the record of truth.

| Field | Type | Notes |
|---|---|---|
| `status` | enum | `QUEUED` \| `RUNNING` \| `SUCCEEDED` \| `FAILED` \| `CANCELLED` \| `INFEASIBLE` (extend per model family only if genuinely needed — prefer reusing these) |
| `detail` | string, optional | Free text, e.g. `"run complete"`, an error summary |

`INFEASIBLE` has turned out to be less solver-specific than it looks, which
is worth recording because it is the argument for reusing these six rather
than growing the enum. `models/panel_fit/` returns it when *every* group in
a panel failed to fit: not `SUCCEEDED`, because zero fits is not a success
and `row_count` cannot disambiguate it (failures are recorded as rows, so an
all-failed run has a healthy-looking count); not `FAILED`, because nothing
went wrong — the run completed, the results are correct and durable, and a
retry would produce the same thing deterministically. "It ran, and the answer
is that there isn't one" is exactly what a MILP means by the word.

The same model is why per-unit outcomes need no envelope change either. A run
where 9 of 48 units failed is a `SUCCEEDED` run whose `progress.payload`
carries `groups_fitted` / `groups_failed` / `failure_counts` on every message
— free-form by design, and a client can tell it apart from a healthy run
without the envelope having a concept of a unit.

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
total. `models/streaming_results/` is the model this was added for, and its
tests fail loudly if the harness stops supporting it. It is no longer the
only one: `models/panel_fit/` emits a chunk every `chunk_size` groups, which
is what keeps a 48-group run from being silent until the end. Two
independent users of a field is roughly where "a feature one model needed"
becomes "part of the contract", so treat it as the latter.

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

## The schema, generated

The tables above are the contract in prose; `schema/envelope.schema.json` is
the same contract a machine can read, generated from `shared/envelope.py` by
`scripts/export_schema.py` and checked against the models in CI-shaped tests
so it cannot drift.

```bash
uv run python scripts/export_schema.py          # regenerate
uv run python scripts/export_schema.py --check  # verify
```

It is a JSON Schema 2020-12 discriminated union keyed on `type`, with the
enums (`LogLevel`, `RunStatus`) published as string enums — so a frontend gets
a union it can narrow on and string-literal types for the enums, rather than
retyping either by hand and going stale the first time one gains a member.

**The frontend does not generate from it, and that was a deliberate call.**
`json-schema-to-typescript` produced output nobody could read — `RunId1`,
`Seq1`, `Type1`, one alias per property occurrence — and carrying none of the
reasoning that makes the contract usable. So `app/client/src/lib/envelope.ts`
is hand-written, and the cost of that (it can silently fall behind
`shared/envelope.py`) is paid by a drift test rather than by discipline:
`app/client/src/lib/envelope.contract.test.ts` checks both directions against
this generated schema — every property and enum member the server can emit is
declared in TypeScript, and nothing declared in TypeScript is absent from the
server or fails to validate against the schema's own
`additionalProperties: false`. Generating instead is still a legitimate
choice; if you take it, pick a filename other than `protocol.ts`. The
frontend already has a `src/transport/protocol.ts` and it is a different
thing entirely — the page↔worker protocol, describing what the transport is
doing rather than what a run emitted.

The app also serves it at `GET /api/schema` (`?kind=envelope|control|protocol`),
and reports `protocol_schema_version` on `/healthz`, so a cached client bundle
and a redeployed server can notice they disagree instead of failing silently
somewhere further downstream.

**Serialization mode, deliberately:** the schema describes what actually goes
out, not what the server is willing to accept.

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
