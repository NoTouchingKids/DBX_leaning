---
name: model-forecasting
description: Design record for models/forecasting/ (BUILT) — an ML forecasting model with a training-loop telemetry shape (epochs, train/val loss) unlike any other model in the platform. Proves the message envelope generalises beyond solver-style progress.
tools: Read, Write, Edit, Bash, Grep, Glob
---

## Status: built. This is the design record, not a to-do list.

`models/forecasting/` exists, is tested under `tests/models/`, and is registered in
`pyproject.toml`, `deploy/requirements/`, `resources/model_forecasting.job.yml`,
`resources/app.yml` and `uc_ddl/002_model_results.sql`. Read the source and
its tests before this file — the module docstring is maintained, this brief
is not. It is kept for the reasoning: **why** this model is in the lineup,
which is the thing the code cannot say about itself.

Two facts this brief predates and every one of these briefs got wrong:

- **This model reads real data.** All eleven models load
  `samples.nyctaxi.trips` through `models/_data`, falling back to a
  deterministic generator when there is no workspace, and carry
  `data_source` / `data_synthetic` / `data_rows` / `data_fallback_reason` on
  every result row so the two runs stay distinguishable afterwards. Where
  this file says "synthetic" or "small fixed problem", read "synthetic
  fallback".
- **There are eleven models, not five.** Any count below is stale. The other
  six were built from `models/README.md` and `/new-model` with no brief at
  all, deliberately — see `docs/parallelization-plan.md`.

This brief was written to build `models/forecasting/`. Read `CLAUDE.md` and
`docs/message-envelope-spec.md` before writing anything.

## What this model is, and why it's in the lineup

A time-series forecasting model — a classical method (e.g. gradient-boosted
trees over lag features, or a simple recurrent/temporal model) trained on
historical data and evaluated on a held-out window. Free Edition ships
sample data (the `samples` catalog — e.g. NYC taxi) that's a reasonable fit
for this without needing to source anything. External data is permitted too
as of 2026-08-24, but it must already be in Unity Catalog: outbound traffic
is restricted to trusted domains, so nothing can be fetched at run time.

Its job in this platform isn't forecasting accuracy — it's proving the
message envelope's `progress` shape works for **training-loop telemetry**
(epochs, train/val loss), which looks nothing like Gurobi's MIP gap or the
scenario model's completion percentage. If this model's progress view can't
be built from the same envelope fields without special-casing, the envelope
itself needs revisiting — that's the point of including this model type at
all.

## Duck-typed surface (no base class, no inheritance)

Same convention as every model here — see `docs/architecture.md`. No
gurobipy model to expose; whatever "the thing that gets trained" is, expose
it and a results accessor the same way the other models do.

## Progress

This is the model that most tests `primary_metric` as validation loss rather
than a solver gap:

- `percent_complete` — epochs completed / total epochs (or training-set
  fraction consumed, if not epoch-based) — knowable and should be populated
- `primary_metric` — validation loss (or whatever the model's chosen
  objective is); `primary_metric_label` — e.g. `"val_loss"`
- `payload` — training-loss (vs. validation), learning rate if it's
  scheduled, or whatever else a richer training-curve view would want later

Emit at most once per epoch (or a few times per second if the model has no
natural epoch boundary) — don't emit per-batch for anything with many
batches per epoch.

## Cancellation

Check the harness's cancellation signal between epochs (or at whatever
natural checkpoint boundary the training loop has). On cancellation, keep
the best model/weights seen so far if the training method naturally tracks
that (e.g. early-stopping-style best-checkpoint tracking) — same
"cancellation is a clean outcome, keep what you have" rule as every other
model.

## Results

The forecast itself: predicted values (and, if meaningful, prediction
intervals) over the held-out/future window, as plain dicts, one row per
forecasted timestep. Include whatever evaluation metrics (MAE, RMSE, etc.)
make sense as either part of the results or worth surfacing via the
envelope's `result.preview`/summary — this is the model where a genuinely
result-shaped preview (a forecast-vs-actual chart) matters most, so keep an
eye on what a useful `preview` payload looks like here even though building
the preview downsampling itself is the harness's job, not this model's.

## Explicit non-goals

- No WebSocket, HTTP, Delta, or SQL code — only `emit(...)`.
- No knowledge of `run_id`/`seq`/timestamps.
- Don't reach for a heavyweight deep-learning stack unless the model
  genuinely needs it — this needs to train fast enough to be useful for
  testing the platform, not win a forecasting competition.

## Tests to write

- Runs standalone against a small fixed dataset, produces a forecast with
  sane shape (right number of timesteps, no NaNs where a real value is
  expected).
- Progress messages populate `percent_complete` and `primary_metric` in a
  way a generic (non-forecasting-aware) frontend progress view could render
  without special-casing.
- Cancellation mid-training still produces a usable (if worse) forecast from
  the best checkpoint available.
