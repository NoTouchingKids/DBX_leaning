# Per-model page design

Status: Decisions taken on both open questions (2026-08-23) — grounded in
the real repo, not the imagined message shape.
Source: `models/*/model.py`, `job/emitter.py`, `job/drivers/gurobi.py`,
`shared/envelope.py`, `shared/downsample.py` as of the merged "Initial
implementation" PR (2026-08-23).

## Why this exists

`frontend/README.md` already says the thing this track needs most is real
envelope traffic, not an imagined message shape. That traffic exists now —
not from a deployed run yet, but from the actual code that produces it. This
document is built by reading that code, not by re-deriving the design from
the original spec. Two places where the original framing doesn't match what
got built were called out explicitly and resolved — see "Decisions" below.

Every page still starts from the generic fallback view already specified in
`.claude/agents/frontend.md` — `percent_complete` / `primary_metric` /
`primary_metric_label`, no special-casing — before any model-specific layer.
That doesn't change here. What follows is the model-specific layer for each
of the five, once real traffic exists to build it against.

## Shared page shell (all five)

- Header: run status, elapsed time, cancel button (disabled with the
  documented escape-hatch message when there's no live WS channel —
  `app/routes/runs.py`'s `CANCEL_ESCAPE_HATCH`).
- Generic strip: `percent_complete` (progress bar, or "not applicable" when
  null — see Gurobi below), `primary_metric` / `primary_metric_label`.
- Log pane: `LogMessage.message`/`level`/`phase`, filtered on
  `client_visible` server-side already (raw Gurobi solver chatter is
  durable-only, not sent live — nothing to filter twice client-side).
- Model-specific panel: everything below.

## Gurobi scheduling

**Progress** (`job/drivers/gurobi.py`, sampled every 2s during `MIP`):
`percent_complete` is always `null` — a MIP-gap-driven solve genuinely isn't
a percentage, and the driver emits `None` rather than guessing. `primary_metric`
is `mip_gap`. `payload`: `best_bound`, `incumbent`, `nodes_explored`,
`nodes_remaining`, `solution_count`. The generic progress bar should render
as indeterminate/hidden for this model specifically — it's not a bug that
`percent_complete` is empty, it's correct.

**MIP-gap chart**: a line chart of `mip_gap` over `elapsed_seconds` (or
`nodes_explored` on the x-axis, which is arguably more informative for a
solver than wall-clock — worth trying both). This is built client-side from
accumulated progress messages, not from `results()` — there is no
`preview_axes` on this model, so its result preview is not a chart series.
Recharts, matching the ADR default.

**Results**: `results()` returns one row per assigned shift —
`{staff, day, shift, cost, preferred}` — a schedule, not a time series. No
`preview_axes` means the preview is an evenly spaced sample of that table
(`shared/downsample.py`'s non-LTTB fallback), not a downsampled curve. Render
as a table or a staff × day grid/calendar (a shift-assignment heatmap is a
natural fit and matches the "solved schedule" mental model) — not a line
chart. This is the one model where reaching for a chart at all would be
forcing a shape onto data that isn't shaped like one.

## Forecasting

**Not a neural net, and the page says so anyway — decision below.** The
original spec said "forecasting can have neural net for animations." The
actual model (`models/forecasting/model.py`) is `SGDRegressor.partial_fit`
over lag features — deliberately light, and the module docstring says so
explicitly: "No deep-learning stack for a platform test." There is no
layer/weight topology to visualize.

**Decision: keep a decorative neural-net animation.** Layered on top of the
real data below, not derived from it — a stylised nodes-and-edges piece that
signals "a model is training" without claiming to represent this specific
`SGDRegressor`'s actual structure. Scope it like any other Three.js use per
the ADR: lazy-loaded on this page only, time-boxed, and the page must
degrade gracefully to the real charts below if it fails or is slow to load —
it is decoration, not the load-bearing visualization.

**Progress**: `percent_complete` from epoch count, `primary_metric` =
`val_loss`. `payload`: `epoch`, `epochs_total`, `train_loss`,
`best_val_loss`, `learning_rate`. This is a genuine training-loop shape —
train/val loss over epochs is a real, standard chart (dual-line Recharts,
`train_loss` and `val_loss` vs `epoch`), and `best_val_loss` gives a natural
"best-so-far" marker to animate in with Framer Motion as it improves.

**Results**: `preview_axes = ("step", "forecast")`, so the preview *is* a
real LTTB-downsampled chart series — a forecast curve over the horizon, each
point also carrying `val_mae`/`val_rmse`/`epochs_trained`. This is the
strongest "reveal" moment of the five models: training curve animates while
running, then the forecast curve draws in on completion. That's a legitimate
Framer Motion moment, and it's the real content the decorative animation
sits alongside — not a replacement for it.

## MCMC

**Decision: extend the model to support real trace + posterior visuals.**
The user's stated goal for this page is to showcase Bayesian inference as a
model class, not just report run health — and acceptance/rhat-over-time,
while real and free, doesn't read as "this is MCMC" the way a trace plot or
a posterior distribution does. Those need per-draw data, which
`models/mcmc/model.py` deliberately doesn't persist today (`results()`'s own
docstring: "Raw draws are deliberately not written: at 8 chains × 800 draws
they are 6,400 rows per parameter of mostly-redundant detail"). The fix is a
small, bounded, deliberate addition — not reversing that decision, working
within it:

1. **Live trace, no new message type.** `payload` is already the model's
   documented extension point (`shared/envelope.py`: "Model-specific extras
   ... a model-specific view can grow into later"). Add one field to the
   existing progress emission — a per-chain snapshot of the current
   `(mu, log_sigma)` position of every walker at that tick. At the default
   `progress_every=50` draws over 800 draws, that's ~16 points per chain —
   coarse, but a genuine trace of chains wandering then settling, and it
   costs nothing new: it rides the cadence and durability the progress path
   already has (backfilled from Delta like any other progress message, not
   a special case).
2. **Posterior view, extend `results()` — bounded, not raw.** Alongside the
   existing 2 summary rows, emit a thinned, capped sample of post-burn-in
   draws (e.g. systematically thinned to a few hundred per chain, capped
   near `PREVIEW_MAX_POINTS` total, matching the bound the platform already
   applies everywhere else) — enough for a real histogram/density per
   parameter, nowhere near the 6,400-row set the model correctly avoids
   today.

This is scoped to `models/mcmc/model.py` and its tests
(`tests/models/test_mcmc.py`) — it does not touch the harness, transport, or
envelope contract, so it doesn't reopen anything already merged as "done and
tested." Treat it as a deliberate, named follow-up on the MCMC track, not a
silent scope change.

**Chain-health view stays too.** `per_chain_acceptance` and `max_rhat` are
still free, still real, and still worth showing — as a smaller diagnostic
panel alongside the trace/posterior, not instead of it. D3, per the ADR, for
whichever of these ends up needing more control than Recharts gives (likely
the trace plot, with its per-chain overlaid series).

**Results panel**: the existing 2-row summary table
(`mean`/`sd`/`q05`/`q50`/`q95`/`rhat`) stays as the at-a-glance numeric
reference next to the trace/posterior visuals, not replaced by them.

## Scenario

**Progress**: the one model where `percent_complete` is genuinely meaningful
(`payload` docstring: "unlike a MIP gap, this is the model where
percent_complete earns its place"). `primary_metric` = `best_objective`,
updating as better scenarios are found — a natural "best so far" counter,
similar in spirit to forecasting's `best_val_loss`. `payload` also carries
`last_scenario`/`last_outcome` — the specific scenario just evaluated, which
could drive a small "currently evaluating: demand=1.1, capacity=1.0..." live
readout distinct from the aggregate metric.

**Results**: `preview_axes = ("scenario_index", "objective")` — a real
LTTB-downsampled series, objective value across the swept grid. A scatter or
line over `scenario_index` is the direct chart; since each row also carries
the full scenario (`demand`, `capacity`, `unit_cost`) and outcome
(`served`, `shortfall`, `idle`), a richer view — colour or facet by one grid
dimension — is possible once there's real data to see whether that's
actually legible or just noisy. Start with the plain objective-over-index
line; treat faceting as a v2 refinement, not a first cut.

## Streaming results

**The incremental-arrival case** — the one model whose results genuinely
change shape mid-run rather than only appearing at the end. Each
`emit("result", rows=..., final=...)` call is a **separate** `ResultMessage`
with its own `chunk_index`, arriving live over the same SSE stream as
progress/log messages (see `models/streaming_results/model.py`'s module
docstring). The frontend must handle a `result`-typed SSE event more than
once per run, appending each chunk rather than treating "result received" as
a terminal, one-time event — this is exactly why `.claude/agents/frontend.md`
already specifies named SSE `event:` listeners per message type rather than
one catch-all handler.

**Progress**: `primary_metric` = `window_mae`, `payload`: `windows_done`,
`windows_total`, `origin` (which point in the series this backtest window
started from).

**Results**: `preview_axes = ("origin", "abs_error")` per chunk — each
chunk's preview is itself an LTTB series. The natural view is a
predicted-vs-actual line chart that **grows** as chunks arrive — new
segments appending to an existing chart is a different animation problem
than forecasting's single reveal-on-completion, and probably the best
demonstration piece for "this is genuinely live" in the whole app.

## Decisions log

1. **Forecasting's "neural net" framing** — kept, as a scoped decorative
   Three.js animation layered on top of the real training-loop/forecast
   charts, not a replacement for them. See "Forecasting" above.
2. **MCMC trace/rank plots** — the model gets a small, deliberate extension
   (per-chain position snapshots in the existing progress payload, plus a
   bounded thinned-draws sample from `results()`) to support real trace and
   posterior visuals, because the page's job is to showcase Bayesian
   inference as a model class, not just report run health. See "MCMC"
   above for the concrete, bounded design.
