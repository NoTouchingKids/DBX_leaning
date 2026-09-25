# Message envelope — the wire contract

This is the contract in prose. It is implemented, in Pydantic, in
`app/shared/envelope.py` (the job imports the same module as `shared.envelope`
— see CLAUDE.md on why it lives under `app/`), the job↔app RPC in
`app/shared/rpc.py`, and the durable part files in `job/telemetry.py`; the
envelope and RPC are published as JSON Schema under `schema/`. If this file
and the code ever disagree, the code is right and this file is stale; say so
and fix it here. Every track — models, job, app, frontend — builds against
this shape.

Four things make up the frozen contract, and a fork of the harness owes the
app all four: the **envelope** (below), **tolerant reading** of it, the **RPC
channel** including its `hello` version handshake, and the **part-file
layout** on the telemetry volume. Each has its own section.

**Do not let a model-building track invent its own message shape.** If this
spec is ambiguous or missing something a model needs, that is a reason to
update this file (and flag it), not to improvise locally — a shape invented
inside `job/models/gurobi_scheduling/` and copied nowhere else is exactly how v1
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
the strong sense; `tests/job/test_harness.py` asserts that of what
the harness writes.

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
than growing the enum. `job/models/panel_fit/` returns it when *every* group in
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
total. `job/models/streaming_results/` is the model this was added for, and its
tests fail loudly if the harness stops supporting it. It is no longer the
only one: `job/models/panel_fit/` emits a chunk every `chunk_size` groups, which
is what keeps a 48-group run from being silent until the end. Two
independent users of a field is roughly where "a feature one model needed"
becomes "part of the contract", so treat it as the latter.

The rows themselves never travel on the message. A model calls
`emit("result", rows=[...])` and the harness writes them to the model's
results table, counts what it wrote into `row_count`, and builds the bounded
`preview`. See `job/models/README.md`.

Per-model result **tables** are separate from this envelope — each model
family has its own results schema in Unity Catalog, governed by its own UC
grants, because different models serve different audiences. The `result`
*message* is only ever a summary/pointer/preview; the full data lives in
that model's own table and is read directly, not replayed through this
envelope.

## The schema, generated

The tables above are the contract in prose; `schema/envelope.schema.json` is
the same contract a machine can read, generated from `app/shared/envelope.py` by
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
`app/shared/envelope.py`) is paid by a drift test rather than by discipline:
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
out, not what the server is willing to accept. That is why it still says
`additionalProperties: false` although readers now ignore unknown fields (next
section): nothing this version *sends* carries an undeclared field, so a
producer validating its own output against the schema is validating the
right thing. A *reader* must not use the schema to reject a record for having
an extra property — that is the tolerant-read rule, and it outranks the
schema for anyone consuming records from a producer that may be newer.

## Reading records: tolerant, per record

Frozen 2026-09-25. What lets a job one minor version ahead of the app still be
observed, and what lets a fork add something without breaking everyone else.

- **An unknown field is ignored, never rejected.** The envelope models use
  `extra="ignore"`: the record is read, the field is dropped. The strictness
  that `extra="forbid"` used to provide moved to the write side —
  `make_message` (what the harness builds every record with) still refuses a
  field the message type does not declare, so a model that misspells a field
  fails loudly in the job instead of losing it silently at the app.
- **An unknown `type` is skipped, never fatal.** The app's parse boundary
  (`app/server/routes/rpc.py`, the `telemetry` handler) handles each record of
  a batch on its own: a record whose `type` it does not know, a record that
  is not an object, and a known type that fails validation are each counted,
  logged once per batch, and skipped. Every other record in the same batch is
  still ingested, and the socket loop never sees an exception from it.
  Nothing is lost by skipping — the part files have every record.
- Missing required fields and out-of-range values are still invalid. Tolerance
  is about *additions*; a record that lacks what this version needs is not one
  it can read.

## The job ↔ app RPC channel

Frozen 2026-09-25. Implemented in `app/shared/rpc.py`; the app's side is
`app/server/routes/rpc.py`, the job's is `job/ws.py`. One WebSocket per run at
`/ws/job/<run_id>`, and **every frame is a WebSocket TEXT frame** carrying one
JSON object — the app reads with `receive_text()`, and a binary frame killed
every run once (CLAUDE.md, "Four failures").

**The framing is JSON-RPC 2.0, exactly.** Every frame has `"jsonrpc": "2.0"`.

| Frame | Members |
|---|---|
| request | `jsonrpc`, `id` (string or integer), `method`, `params` (object) |
| notification | `jsonrpc`, `method`, `params` — no `id`, and it never gets a reply |
| success | `jsonrpc`, `id`, `result` (any JSON, including `null`) |
| error | `jsonrpc`, `id` (`null` if the request could not be parsed), `error: {code, message, data?}` |

Two deliberate subsets of JSON-RPC: `params` is always an object (positional
arrays are refused with `-32602`), and batch arrays are not accepted. A frame
carrying both `result` and `error`, or an `error` without an integer `code`
and string `message`, is refused as `-32600`. Error codes: JSON-RPC's own
(`-32700`, `-32600`, `-32601`, `-32602`, `-32603`) plus ours outside the
reserved range — `-31001` unknown run, `-31002` records gone,
`-31003` incompatible protocol version.

The method set is six — `hello`, `telemetry`, `bye` (job → app), `cancel`,
`replay` (app → job), `ping` (either) — and `schema/control.schema.json`
publishes each one's kind and direction.

### `hello`: the version handshake

Modelled on LSP's `initialize`. It is the job's **first frame** on every
connection, and a request:

| `params` | |
|---|---|
| `run_id` | string |
| `next_seq` | integer — the seq the job is picking up from |
| `protocol_version` | string `"MAJOR.MINOR"` — **required** |
| `capabilities` | object — one key per method this side answers (`cancel`, `replay`, `ping`), each value that method's options (`{}` today) |

The success `result` is `{observed, run_id, protocol_version, capabilities}`,
the last two being the app's own. The current version is `PROTOCOL_VERSION =
"1.0"` in `app/shared/rpc.py`, and `schema/control.schema.json` publishes it
as `x-protocol-version`.

**The compatibility rule — the whole of it:** the app accepts a job whose
MAJOR equals the app's MAJOR and whose MINOR is less than or equal to the
app's MINOR. Numbers compare numerically (`1.10` > `1.9`). So an app at 1.3
observes jobs at 1.0–1.3; an app redeployed ahead of its jobs keeps observing
them, and a job ahead of its app runs unobserved until the app catches up.
The version is a string of two non-negative integers with no leading zeros —
`"1"`, `"1.0.0"`, `"v1.0"` and the JSON number `1.0` are all malformed.

Bump MINOR for an addition an older app can ignore under the tolerant-read
rule (an optional field, a new `type`, a method the other side need not
call). Bump MAJOR, and reset MINOR, for anything an older app would misread.

**What a refusal looks like:**

- `protocol_version` missing or malformed → error `-32602`; well-formed but
  outside the rule → error `-31003`. Both carry
  `data: {app_protocol_version, rule}`, the latter also `job_protocol_version`.
- The app then **closes the socket** with code 1008 and a reason beginning
  `hello refused:`.
- The app processes nothing on a connection until a `hello` is accepted: a
  request sent first is answered `-32600` ("send hello before anything
  else"), a notification sent first is dropped, and the job is not reachable
  for `cancel` or `replay` until it has attached.
- **The job treats a refusal as "run unobserved", never as a run failure.** It
  logs the app's error once, stops — no reconnect, since retrying an
  incompatible version can only get the same answer — and the run continues
  with its durable path untouched. The job waits for `hello`'s answer before
  streaming anything, so a refusal can never be mistaken for a dropped
  connection and retried.

## The durable record: telemetry part files

Frozen 2026-09-25. Written by `job/telemetry.py::PartFileWriter`. Read today
by `replay`, and later by the Slice 4 ingestion job; this section is the
promise both of them build on.

- **Directory:** `<telemetry root>/runs/<run_id>/`. The root is the job's
  `DBX_TELEMETRY_VOLUME`, by default `/Volumes/main/dbx_leaning/telemetry`
  (`uc_ddl/004_telemetry_volume.sql`). One directory per run; the harness
  writes nothing into it but part files.
- **File name:** `part-NNNNN.jsonl` — five-digit, zero-padded, numbered from
  `part-00001` in the order they were closed. Zero-padding keeps
  lexicographic and numeric order the same through `part-99999`.
- **Framing:** JSON Lines, UTF-8. One record per line, each line a complete
  JSON object terminated by `\n`, written compactly (`separators=(",", ":")`).
  Each record is exactly an envelope message in its JSON form
  (`model_dump(mode="json")`: enums as their string values) — the same shape
  the job sends in `telemetry` and the app serves over SSE. **Not msgpack:**
  that was v3, and v4 moved everything to JSON (see Encoding).
- **Ordering:** records within a part are in the order they were emitted,
  and part numbers increase over the run, but a reader must order by `seq`,
  not by file position — `seq` is the contract, and `replay` sorts by it.
  `seq` values are unique within a run; a reader that sees one twice may
  treat the copies as the same record.
- **A part is durable only once it is closed.** Each part is written whole
  and closed in one operation, never appended to afterwards. On a UC volume a
  file does not exist to another reader until it is closed, and a record that
  has not yet been rolled into a closed part is lost if the job dies. Parts
  roll on size or age (by default 1 MB or 30 s, whichever first) and at end
  of run; the sizes are tuning, not contract.
- **One terminal status per run.** A run that completes ends with exactly one
  `status` record with `terminal: true`, written by the harness as the run's
  last record (highest `seq`), after the final roll — and the harness refuses
  to report `SUCCEEDED` if any record failed to reach a closed part. A model
  must not emit a `terminal: true` status itself. A run whose process died has
  *no* terminal status in its parts, which is how a reader tells "crashed"
  from "finished".

## Encoding (a delivery detail, not part of the contract)

- **JSON everywhere**, since v4: JSON-RPC text frames job ↔ app, JSON Lines
  in the telemetry part files, JSON on the SSE stream to the browser. One
  codec, readable in devtools and in a part file an operator opens by hand,
  and `replay` parses the same bytes that were written. The msgpack this
  section used to describe (job → app and in the Delta write buffer) is v3
  and is gone, dependency and all — `app/shared/codec.py` says why.
- The envelope's job is to define valid *shape*. Whatever encodes it must
  produce the same logical content — that interchangeability is the test that
  the boundary between "protocol" and "serialisation" is drawn in the right
  place.
- Validation (Pydantic) lives with whichever side is deserialising, not
  inside the encoding step itself.

## What a model actually sees

A model never imports this spec, WebSockets, or anything
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

Added while implementing `shared/`. `job/models/streaming_results/` needs to emit
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

### 2026-09-25 — the wire freeze (v5 Phase 1)

Four changes and one piece of writing-down, so that the wire — not the
harness — is the compatibility promise a fork owes the app.

1. **Tolerant read.** Envelope models went from `extra="forbid"` to
   `extra="ignore"`: an unknown field is dropped, not rejected. The write side
   stays strict — `make_message` still refuses an undeclared field, now with a
   `ValueError` naming it rather than a Pydantic `ValidationError`. The
   published schema is unchanged and still says `additionalProperties: false`
   (it describes output). See "Reading records".
2. **Unknown `type` is skipped, never fatal**, at the app's `telemetry` parse
   boundary, per record; the rest of the batch is still ingested. The code
   already survived a bad record — what changed is that an unknown type is
   now told apart from a malformed record, and both are counted and logged
   once per batch instead of once per record.
3. **`hello` carries a mandatory `protocol_version`** (`"1.0"`), enforced by
   the rule *job MAJOR == app MAJOR and job MINOR <= app MINOR*. A refused
   `hello` gets a JSON-RPC error (`-32602` missing/malformed, `-31003`
   incompatible — a new code) and the socket is closed with 1008. The app now
   processes nothing before an accepted `hello`, and registers the job for
   `cancel`/`replay` only then. The job waits for `hello`'s answer before
   streaming, and treats a refusal as "unobserved, stop retrying". **Breaking
   for any client that does not send the field** — the only one is this
   repo's `job/ws.py`, which does, so job and app must deploy together once.
4. **JSON-RPC 2.0 formalised.** The frames already had the 2.0 shape; what
   changed is that `parse` now refuses a response with both `result` and
   `error`, an `error` without an integer `code` and string `message`, and an
   `id` that is not a string, integer or absent. `hello` follows LSP's
   capability exchange: `capabilities` in the params (the job's) and in the
   result (the app's), alongside each side's `protocol_version`.
5. **Written down, not changed:** the part-file layout, as part of the frozen
   contract (see "The durable record"). The Encoding section's msgpack claim
   was stale since v4 and is corrected: everything is JSON. The top of this
   file pointed at `shared/envelope.py`; it is `app/shared/envelope.py`.
