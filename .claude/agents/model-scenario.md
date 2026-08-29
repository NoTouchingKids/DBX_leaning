---
name: model-scenario
description: Design record for job/models/scenario/ (BUILT) — a cheap, deterministic scenario-sweep model. Exercises fan-out concurrency and the platform's 5-concurrent-job-task ceiling with many small, fast runs rather than one long one.
tools: Read, Write, Edit, Bash, Grep, Glob
---

## Status: built. This is the design record, not a to-do list.

`job/models/scenario/` exists, is tested under `tests/models/`, and is registered in
`pyproject.toml`, `deploy/requirements/`, `resources/model_scenario.job.yml`,
`resources/app.yml` and `uc_ddl/002_model_results.sql`. Read the source and
its tests before this file — the module docstring is maintained, this brief
is not. It is kept for the reasoning: **why** this model is in the lineup,
which is the thing the code cannot say about itself.

Two facts this brief predates and every one of these briefs got wrong:

- **This model reads real data.** All ten models read a real Unity Catalog
  table through `job/models/_data` — eight of them `samples.nyctaxi.trips`,
  this one included — falling back to a deterministic generator when there is
  no workspace, and carry `data_source` / `data_synthetic` / `data_rows` /
  `data_fallback_reason` on every result row so the two runs stay
  distinguishable afterwards. Where
  this file says "synthetic" or "small fixed problem", read "synthetic
  fallback".
- **There are ten models, and four of them have a brief.** Any count below is
  stale. The other six were built from `job/models/README.md` and `/new-model`
  with no brief at all, deliberately — see `docs/parallelization-plan.md`.

This brief was written to build `job/models/scenario/`. Read `CLAUDE.md` and
`docs/message-envelope-spec.md` before writing anything.

## What this model is, and why it's in the lineup

Every other model in this platform is one long-running process per run. This
one is deliberately the opposite: **cheap, deterministic, and fast — the
point is to have many small runs in flight at once**, not to stress any
single run's telemetry. It's what proves out fan-out behaviour under Free
Edition's hard ceiling of **5 concurrent job tasks per account** (see
`docs/free-edition-constraints.md`) — a case none of the other models
naturally exercise, since they're each built to be one long thing.

Concretely: a scenario sweep over a parameter space — vary a handful of
inputs (e.g. demand levels, resource availability, a cost assumption) across
N scenarios, evaluate some deterministic objective or small optimisation per
scenario, and report results per scenario. Keep the per-scenario computation
genuinely cheap (milliseconds to low seconds) — the value here is exercising
many fast runs and the message envelope at high message-rate-per-wall-clock-
second, not modelling depth.

## Duck-typed surface (no base class, no inheritance)

Same convention as every other model in this platform (see
`docs/architecture.md`) — the harness discovers a build step, a way to run,
a results accessor, by name, not by inheritance. This model likely has no
Gurobi model to expose and no callback to compose — it runs as plain Python,
which the harness should handle as a simpler case (check what `job/`'s
discovery logic actually requires once it exists; if something it needs
doesn't make sense for a non-Gurobi model, that's worth flagging rather than
faking it).

## Progress

Unlike Gurobi's MIP gap, this model's "progress" is naturally **a completion
percentage** — scenarios evaluated so far out of the total. Populate:

- `percent_complete` — genuinely knowable here, unlike MIP progress; this is
  the model where that field gets real use
- `primary_metric` / `primary_metric_label` — whatever summary makes sense
  running (e.g. best-objective-so-far across evaluated scenarios), or leave
  null if nothing meaningful exists mid-run
- `payload` — per-scenario detail if useful for a richer view later (e.g.
  which scenario just completed and its result)

Emit progress at a sensible cadence relative to how fast scenarios complete
— if a scenario takes milliseconds, emitting progress after every single one
would flood the channel; batch progress updates (e.g. every N scenarios or
every second, whichever comes first).

## Cancellation

Check the harness's cancellation signal between scenarios (not mid-scenario
— each scenario should be fast enough that this granularity is fine).
Support returning whatever scenarios have already completed as results on
cancellation — same rule as every other model: results are not best-effort,
write what exists.

## Results

One row per scenario (its parameters and its outcome), as plain dicts.

## Explicit non-goals

- No WebSocket, HTTP, Delta, or SQL code — only `emit(...)`.
- No knowledge of `run_id`/`seq`/timestamps.
- Don't artificially slow this down to "look more like" the other models —
  its value to the platform is specifically being fast and numerous.

## Tests to write

- Runs standalone, no harness, over a small fixed scenario set, produces
  deterministic results (same inputs → same outputs, every time — this
  model's whole value depends on being deterministic).
- Cancellation mid-sweep returns completed-scenario results, not none.
- Progress batching doesn't flood — assert a reasonable message count for a
  sweep of N scenarios (not a message-per-scenario for large N).
