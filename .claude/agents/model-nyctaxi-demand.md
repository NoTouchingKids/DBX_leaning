---
name: model-nyctaxi-demand
description: "UNBUILT PROPOSAL — job/models/nyctaxi_demand/ does not exist and its original justification is void. Do not dispatch this to write code without reading the header first."
tools: Read, Write, Edit, Bash, Grep, Glob
---

## STOP — this describes a model that was never built, on premises that are now false

`job/models/nyctaxi_demand/` does not exist, and nothing in the repo references
it. This file is kept as a **proposal**, not as a work order. It is the only
brief here for an unbuilt model; every other `.claude/agents/model-*.md`
documents something on disk.

**Its central justification is gone.** This brief was written when the five
models then in existence all used synthetic in-memory data, and it proposed
closing that gap. That gap closed a different way: every one of the ten models
now reads a real Unity Catalog table through `job/models/_data` and the job's
Spark session — eight from `samples.nyctaxi.trips`, `ortools_jobshop` from
`samples.bakehouse.sales_transactions`, `panel_fit` from a panel table nobody
has landed — falling back to a deterministic generator off-platform — and every
result row carries `data_source` / `data_synthetic` / `data_rows` /
`data_fallback_reason` so the two paths stay distinguishable. So the
paragraph below claiming "none of them reads a real Unity Catalog table" is
simply wrong now, and "the first to genuinely need Spark/UC as an input" is
no longer available to be first at.

**What is still unclaimed, if this is ever built.** Two things, and they are
the only reasons left:

1. *Spark stage-level progress.* Every model here reports progress from
   inside its own loop. Nothing drives `percent_complete` from
   `SparkContext.statusTracker()` or a `SparkListener` — a genuinely
   different way to populate the same envelope fields, and the one thing in
   this brief that no other model has proven.
2. *A 2-D result shape.* Every results table on disk is a flat series, a
   solution, a decision table or one row per unit. An hour × zone matrix
   wants a heatmap, and neither the `preview_axes` LTTB path nor the frontend
   has anything for that.

Neither is a gap that hurts. Build this on the strength of those two or not
at all — and if you do, follow `/new-model`, which is the current and
maintained procedure, in preference to the sections below. Delete this file
the day the model exists (its brief would be redundant) or the day someone
decides it never will.

Everything below this line is the original 2026 proposal, preserved
unedited apart from this header. Read it as history.

---

Read `CLAUDE.md`, `docs/message-envelope-spec.md`, and
`docs/free-edition-constraints.md` before writing anything.

## What this model is, and why it's in the lineup

RETRACTED PREMISE — this paragraph was true when written and is not now.
Every model in the repo reads a real Unity Catalog table through
`job/models/_data`, with provenance on every result row; the fallback to a
deterministic generator is a degraded path that says so, not the design.
`docs/model-expansion-and-packaging.md`'s status note 1 records the same
retraction. What follows is preserved as the original argument.

Every one of the first five models deliberately uses synthetic, in-memory
data — each one's own docstring says so ("no deep-learning stack for a
platform test," "small on purpose"). None of them reads a real Unity
Catalog table, and none of them uses the job's Spark session for anything
but the Delta-write fallback in `job/delta.py`. This model closes that gap:
it's the first to genuinely need Spark/UC as an **input**, not just as the
durable-write path.

Concretely: read `samples.nyctaxi.trips` (verified present in the `samples`
catalog — see `docs/model-expansion-and-packaging.md`) via the Spark session
the job already has, and compute a rolling aggregation — pickups per hour ×
pickup zone is the reference shape, but the exact grouping is this model's
call. The point isn't the aggregation's sophistication; it's proving a job
can cheaply read real UC data through Spark without ever touching the SQL
warehouse (the app's "no Spark from the app" rule is an app-track rule —
this model runs inside a job, which already has a Spark session for
exactly this kind of use).

## Duck-typed surface (no base class, no inheritance)

Same convention as every other model (see `docs/architecture.md`). This
model has no Gurobi model to expose and no callback slot to compose — check
what `job/loader.py`'s discovery actually requires (`build`/`run`/`results`
by name) and satisfy that, the same shape `job/models/scenario/model.py` and
the others already do.

**Where this model is genuinely different from the other five:** it needs a
`SparkSession`. Get it the way any Databricks serverless job already can —
check how `job/delta.py`'s Spark fallback obtains its session and reuse
that path rather than inventing a second way to acquire one. This is still
within the duck-typed model's own `build()`/`run()` — it is not a harness
change.

## Progress

Spark doesn't offer a single composable callback the way Gurobi does, so
this model's progress telemetry has to come from somewhere else — most
likely polling `SparkContext.statusTracker()` (active job/stage IDs, tasks
completed vs total) from a light background loop, or a registered
`SparkListener` if that turns out cleaner. Whichever mechanism, populate:

- `percent_complete` — tasks completed / tasks total for the active stage,
  where knowable; null if between stages rather than a guess (same
  discipline as Gurobi's MIP gap: don't fabricate a percentage the data
  doesn't support)
- `primary_metric` / `primary_metric_label` — something like rows processed
  so far, if cheaply available; leave null if nothing meaningful exists
  mid-run
- `payload` — stage name/id, task counts, whatever Spark's own progress
  APIs expose that a richer view could use later

Emit at a sensible cadence relative to how fast stages complete — same
batching discipline as `job/models/scenario/model.py`, not a message per task.

## Cancellation

Check the harness's cancellation signal at a natural boundary — between
stages, not mid-task. Whether Spark's own job can be cancelled cleanly
mid-query (`SparkContext.cancelJobGroup` or similar) is worth verifying
against the actual job/stage structure this model ends up with, rather than
assumed; if a clean mid-query cancel isn't practical, letting the current
stage finish before checking is an acceptable, documented trade-off — name
it rather than silently accepting whatever Spark does by default.

## Results

Rows shaped as `{hour, zone, pickup_count, ...}` (or whatever the chosen
grouping is) — a time × zone matrix, not a single time series. This is
deliberately a new result shape for the platform — none of the existing five
produces anything like it, and it wants a heatmap/calendar view rather than a
line chart. The frontend design for that is not written yet; that track is on
hold (`app/client/README.md`).
Whether `preview_axes` (the two-column LTTB downsampling every other model
uses) makes sense for a 2D grouping like this is worth a real look — it may
not fit cleanly, in which case falling back to the even-sample preview
(like Gurobi's schedule table) is the honest choice, not a gap to force
`preview_axes` into.

## Explicit non-goals

- No WebSocket, HTTP, Delta-write, or SQL-warehouse code — only `emit(...)`
  for output. Reading `samples.nyctaxi.trips` via Spark is this model's one
  deliberate exception to "models never touch data access themselves," and
  it's an input-side exception only — writing results/progress out still
  goes through the same `emit()` callback every other model uses, unchanged.
- Don't reach for the SQL warehouse for anything. If Spark can't answer a
  question this model needs, that's a design constraint to work within, not
  a reason to add a warehouse dependency this platform has deliberately
  avoided everywhere else.
- Don't scope this model's grouping/aggregation ambitiously before the
  telemetry and results shape are proven end to end — get pickups-per-hour-
  per-zone working and streaming correctly first.

## Tests to write

- Runs standalone against a small fixture (not the full `samples.nyctaxi.trips`
  table — a local Spark session over a tiny synthetic DataFrame with the
  same schema shape), no harness involved, same pattern as the other four
  models' tests.
- Progress emission reflects real stage/task counts, not a fabricated
  percentage, and stays within a reasonable message cadence for a
  multi-stage aggregation.
- Cancellation at a stage boundary returns whatever aggregation has already
  completed as results, same "results are not best-effort" rule as every
  other model.
- Results rows match the declared shape and, if `preview_axes` is used,
  downsample sensibly; if not, confirm the even-sample fallback produces a
  usable preview rather than an empty or nonsensical one.
