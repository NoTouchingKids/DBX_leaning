---
name: model-streaming-results
description: Design record for job/models/streaming_results/ (BUILT) — chosen to exercise partial/chunked RESULT messages, which nothing else did at the time (job/models/panel_fit now chunks too). A rolling-origin backtest or chunked batch inference.
tools: Read, Write, Edit, Bash, Grep, Glob
---

## Status: built. This is the design record, not a to-do list.

`job/models/streaming_results/` exists, is tested under `tests/models/`, and is registered in
`pyproject.toml`, `deploy/requirements/`, `resources/model_streaming_results.job.yml`,
`resources/app.yml` and `uc_ddl/002_model_results.sql`. Read the source and
its tests before this file — the module docstring is maintained, this brief
is not. It is kept for the reasoning: **why** this model is in the lineup,
which is the thing the code cannot say about itself.

Two facts this brief predates and every one of these briefs got wrong:

- **This model reads real data.** All eleven models load
  `samples.nyctaxi.trips` through `job/models/_data`, falling back to a
  deterministic generator when there is no workspace, and carry
  `data_source` / `data_synthetic` / `data_rows` / `data_fallback_reason` on
  every result row so the two runs stay distinguishable afterwards. Where
  this file says "synthetic" or "small fixed problem", read "synthetic
  fallback".
- **There are eleven models, not five.** Any count below is stale. The other
  six were built from `job/models/README.md` and `/new-model` with no brief at
  all, deliberately — see `docs/parallelization-plan.md`.

This brief was written to build `job/models/streaming_results/`. Read `CLAUDE.md` and
`docs/message-envelope-spec.md` before writing anything.

## What this model is, and why it's in the lineup

Every other model in this platform produces results **once, at the end**
(or once, at cancellation). This model is chosen specifically because it
doesn't: it produces results **incrementally, in chunks, while still
running** — and at the time nothing else in the platform exercised that path.
(`job/models/panel_fit` now chunks as well, flushing per batch of group fits. It
was built after this one and on this one's proof, so the justification below
still stands; the word "only" no longer does.) If the
envelope's `result` message and the harness's "write results whenever the
model produces them" rule only work for the once-at-the-end case, that's a
gap this model is meant to expose before it becomes a real limitation.

Concretely: a rolling-origin backtest (re-fit and forecast repeatedly across
a sliding window, emitting each window's result as it completes) or chunked
batch inference (score a large input set in batches, emitting each batch's
predictions as it finishes) — either fits. Pick whichever is less work to
build well; the platform-proving value is the same either way.

## Duck-typed surface (no base class, no inheritance)

Same convention as every model here — see `docs/architecture.md`.

## The thing that makes this model different: multiple result emissions

Where every other model calls whatever produces a `result` message once
(at the end, or at cancellation), this model calls it **once per chunk** —
after each backtest window or each inference batch completes. Check how
`job/`'s harness handles this before assuming: does emitting `result`
multiple times per run append to a running results table cleanly, does each
emission need a chunk identifier distinct from the run's own `seq`, does the
final terminal-status write need to wait for the last chunk? These are
exactly the questions this model exists to surface — if the answer isn't
already clear from `job/`'s implementation, that's worth raising as a gap in
the harness or the envelope spec, not working around silently in this model.

Whatever the resolution, this model's own responsibility stays simple: call
emit-a-result once per completed chunk, with that chunk's rows and an
accurate `row_count` for that chunk (not a running total, unless the harness
specifically expects that — confirm rather than assume).

## Progress

Standard `percent_complete` (chunks completed / total chunks) works cleanly
here — this model doesn't need the same `payload`-heavy treatment MCMC does.

- `percent_complete` — chunks done / total chunks
- `primary_metric` — whatever per-chunk quality signal makes sense (e.g.
  backtest error for the most recently completed window)
- `payload` — optional per-chunk detail

## Cancellation

Check the harness's cancellation signal between chunks. On cancellation,
whatever chunks have already been emitted as results stand — there's
nothing extra to "finalize," since results were already streamed
incrementally rather than held until the end. This is in some ways the
cleanest cancellation story of any model here, precisely because results
were never being accumulated in memory waiting for a single final write.

## Explicit non-goals

- No WebSocket, HTTP, Delta, or SQL code — only `emit(...)`.
- No knowledge of `run_id`/`seq`/timestamps.
- Don't retrofit this into a once-at-the-end model to make it simpler — the
  entire point is testing the incremental case.

## Tests to write

- Runs standalone, no harness, over a small fixed input, produces multiple
  result emissions (assert more than one, not just that results eventually
  appear).
- Each chunk's result carries an accurate row count for that chunk.
- Cancellation mid-run leaves already-emitted chunks as valid results, with
  no attempt to re-emit or roll them back.
- Explicitly flag (in a comment or a test that fails loudly) if `job/`'s
  harness turns out not to support multiple `result` emissions per run
  cleanly — this is the one model most likely to find that gap.
