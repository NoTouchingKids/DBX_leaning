/**
 * Per-model config surface and progress payload shapes.
 *
 * Hand-derived by reading every `cfg.get(...)` call in each
 * `models/<name>/model.py`
 * and every `emit("progress", ...)` payload. Five models at commit 114f4bb;
 * re-derived across all nine at commit 0fbde20.
 *
 * WHY THIS FILE EXISTS AND WHAT IT IS NOT:
 *
 * `TriggerRequest.config` is `dict[str, Any]`, passed verbatim into
 * DBX_MODEL_CONFIG. The server validates NOTHING. So there is no schema to
 * generate from — this file IS the schema, and it is a frontend invention
 * grounded in what each model actually reads. It can drift. When a model
 * changes its `cfg.get` calls, this file must be updated by hand, and there
 * is no test that will tell you. Re-derive it whenever `models/` changes.
 *
 * Corollary: do not build a generic schema-driven form generator yet. Nine
 * hand-written forms first; generalise once the shape is proven, not before.
 * Nine is past the point where that advice would normally flip — it has not,
 * because four of the nine have a field the generator would have to special-
 * case anyway (`scenario`'s nested grid, `bayesian_ab`'s two structured
 * overrides and its enum, `neural_net`'s layer list, `gurobi_routing`'s
 * licence-derived cap on `stop_count`).
 */

/* ================================================================== *
 * Field descriptors — enough to drive a form, not a validation library
 * ================================================================== */

export type FieldKind = "int" | "float" | "string" | "bool" | "number-list";

export interface ConfigField {
  key: string;
  label: string;
  kind: FieldKind;
  /** `undefined` means the model reads it with no default — i.e. omitting the
   *  key is meaningful (auto / no limit / solver default). Send nothing
   *  rather than sending a zero. */
  default?: number | string | boolean | number[];
  /** Advanced fields go behind a disclosure, not on the main form. */
  advanced?: boolean;
  hint?: string;
}

/**
 * What `models/_data`'s `Dataset.describe()` puts on a payload or a result
 * row, for any model that carries provenance.
 *
 * Load-bearing, not bookkeeping: it is the only thing distinguishing a chart
 * of measured data from a chart of invented data. Every loader here falls
 * back to a deterministic generator when the real table is missing, and that
 * fallback is silent by design — a run that never touched Unity Catalog looks
 * exactly like one that did, apart from these four fields.
 *
 * `data_fallback_reason` is FREE TEXT and varies by environment — offline it
 * reads "no Spark session (not running on a Databricks cluster)", on a
 * workspace it names the table. Render it; never pattern-match it.
 */
export interface ProvenanceFields {
  data_source: string;
  data_synthetic: boolean | null;
  data_rows: number;
  data_fallback_reason: string | null;
}

export interface ModelSpec {
  name: string;
  label: string;
  fields: ConfigField[];
}

/* ================================================================== *
 * gurobi_scheduling
 * ================================================================== */

export const GUROBI_SCHEDULING: ModelSpec = {
  name: "gurobi_scheduling",
  label: "Gurobi scheduling",
  fields: [
    { key: "staff_count", label: "Staff count", kind: "int", default: 20 },
    { key: "days", label: "Days", kind: "int", default: 14 },
    { key: "max_shifts_per_staff", label: "Max shifts / staff", kind: "int", default: 10 },
    { key: "seed", label: "Seed", kind: "int", default: 20260822 },
    {
      key: "use_sample_data",
      label: "Use sample-catalog demand",
      kind: "bool",
      default: true,
      hint: "On = a real hourly demand curve from the samples catalog.",
    },
    { key: "trips_per_staff", label: "Trips per staff", kind: "float", advanced: true, hint: "Omit for auto." },
    { key: "time_limit_s", label: "Time limit (s)", kind: "float", advanced: true, hint: "Omit for no limit." },
    { key: "mip_gap", label: "MIP gap target", kind: "float", advanced: true, hint: "Omit for the solver default." },
  ],
};

/** From `job/drivers/gurobi.py`, sampled every ~2s during the MIP phase. */
export interface GurobiProgressPayload {
  /**
   * NULLABLE, both of them. `job/drivers/gurobi.py::_real_or_none` maps
   * Gurobi's ±1e100 pre-incumbent sentinel to `None`, which arrives as JSON
   * null — so every message before the first feasible solution has
   * `incumbent: null`, and `best_bound` is null until the root relaxation
   * solves.
   *
   * They were declared non-null here, which is the kind of error that does
   * not throw: a chart trusting it plots null as 0 and drags the objective
   * axis to the origin for the whole run. Filter with a finite check and let
   * the pre-feasible stretch be a gap.
   */
  best_bound: number | null;
  incumbent: number | null;
  nodes_explored: number;
  nodes_remaining: number;
  solution_count: number;
}
/** `percent_complete` is ALWAYS null here. `primary_metric` is `mip_gap`.
 *  There is NO per-cell / candidate-schedule data — which is precisely why
 *  the schedule grid's running-state cells are decorative. */

/* ================================================================== *
 * gurobi_routing
 * ================================================================== */

/** `models/gurobi_routing/instance.py::MAX_STOPS`. Not a taste limit: the
 *  formulation is edge-based, so n stops cost n(n-1)/2 + n variables, and 55
 *  is where that still clears the bundled restricted licence's 2000-variable
 *  cap with headroom (62 would already be 1953). `build_instance` RAISES
 *  above this rather than letting Gurobi reject the model at solve time — so
 *  enforcing it in the form turns a failed run into a disabled submit. */
export const ROUTING_MAX_STOPS = 55;

export const GUROBI_ROUTING: ModelSpec = {
  name: "gurobi_routing",
  label: "Gurobi routing",
  fields: [
    // NOTE: the model also reads `instance` — and `pop`s it, so it never
    // reaches `build_instance`. It takes a constructed Python object, which
    // cannot survive a JSON round trip. It is a test/embedding seam, not a
    // form field. Do not surface it.
    { key: "stop_count", label: "Stops", kind: "int", default: 24, hint: `Max ${ROUTING_MAX_STOPS}.` },
    { key: "vehicles", label: "Vehicles", kind: "int", default: 3 },
    { key: "seed", label: "Seed", kind: "int", default: 20260823 },
    {
      key: "use_sample_data",
      label: "Use sample-catalog trips",
      kind: "bool",
      default: true,
      hint: "On = stop radii, service times and the price of distance all come from real trips.",
    },
    {
      key: "time_limit_s",
      label: "Time limit (s)",
      kind: "float",
      default: 120,
      advanced: true,
      // The one place these two Gurobi models genuinely differ, and it is
      // easy to copy the wrong way: scheduling omits this key and gets no
      // limit, routing omits it and gets 120s. Falsy (0) disables it here.
      hint: "Defaults to 120s — unlike Gurobi scheduling, omitting this does NOT mean unlimited. Send 0 for no limit.",
    },
    { key: "mip_gap", label: "MIP gap target", kind: "float", advanced: true, hint: "Omit for the solver default." },
  ],
};

/** Routing emits NO progress of its own. Its one callback slot goes to lazy
 *  rounded-capacity cuts, and the same `job/drivers/gurobi.py` sampler that
 *  drives scheduling composes around it — so the live shape is identical, and
 *  so is the always-null `percent_complete` and the `mip_gap` metric. */
export type GurobiRoutingProgressPayload = GurobiProgressPayload;
/** Two things the UI has to know that the payload does not carry:
 *
 *  1. `cuts_added` / `separation_calls` — how much of the run was separation
 *     rather than search — exist on the model and reach the results and the
 *     logs, NEVER a progress message. Do not build a live cut counter.
 *  2. INFEASIBLE is a reachable terminal status here in a way it is not for
 *     scheduling: too few vehicles for the service minutes on offer is a
 *     one-field mistake a user makes on the form. Render it as a distinct
 *     outcome with the vehicles/capacity numbers, not as a generic failure —
 *     it is a correct answer to a badly posed question. */

/* ================================================================== *
 * forecasting
 * ================================================================== */

export const FORECASTING: ModelSpec = {
  name: "forecasting",
  label: "Forecasting",
  fields: [
    { key: "days", label: "Days of history", kind: "int", default: 60 },
    { key: "column", label: "Target column", kind: "string", default: "trips" },
    { key: "lags", label: "Lag features", kind: "int", default: 24 },
    { key: "horizon", label: "Forecast horizon", kind: "int", default: 48 },
    { key: "epochs", label: "Epochs", kind: "int", default: 40 },
    { key: "seed", label: "Seed", kind: "int", default: 7 },
    { key: "series", label: "Series override", kind: "number-list", advanced: true },
  ],
};

export interface ForecastingProgressPayload {
  epoch: number;
  epochs_total: number;
  train_loss: number;
  best_val_loss: number;
  learning_rate: number;
  /** Lets a live view badge "synthetic" before results exist. */
  data_synthetic: boolean | null;
}
/** CORRECTION to earlier design notes: `val_loss` is NOT in the payload — it
 *  is `primary_metric` (label "val_loss"). The training-loss chart therefore
 *  plots `payload.train_loss` against `primary_metric`, not two payload keys.
 *  `percent_complete` IS populated here: 100*(epoch+1)/epochs. */

/* ================================================================== *
 * mcmc
 * ================================================================== */

export const MCMC: ModelSpec = {
  name: "mcmc",
  label: "MCMC",
  fields: [
    { key: "rows", label: "Rows sampled", kind: "int", default: 2000 },
    { key: "x_column", label: "x column", kind: "string", default: "trip_distance" },
    { key: "y_column", label: "y column", kind: "string", default: "fare_amount" },
    { key: "chains", label: "Chains", kind: "int", default: 8 },
    { key: "draws", label: "Draws", kind: "int", default: 3000 },
    { key: "burn_in", label: "Burn-in", kind: "int", default: 1000 },
    { key: "seed", label: "Sampler seed", kind: "int", default: 11 },
    { key: "data", label: "Data override", kind: "string", advanced: true },
    { key: "data_seed", label: "Data seed", kind: "int", default: 11, advanced: true },
    { key: "progress_every", label: "Progress every N draws", kind: "int", default: 200, advanced: true },
    { key: "progress_every_s", label: "Progress every N seconds", kind: "float", default: 2.0, advanced: true },
  ],
};

export interface McmcProgressPayload {
  draws_done: number;
  draws_total: number;
  chains: number;
  parameters: string[];
  post_burn_in_draws: number;
  mean_acceptance: number;
  min_acceptance: number;
  /** emcee's analogue of a divergence count: a chain accepting nothing is
   *  not exploring. The single most diagnostic number on this page. */
  stuck_chains: number;
  per_chain_acceptance: number[];
  /**
   * Each chain's current position, as one list of parameter values per chain
   * in `parameters` order. This is what the live trace chart is drawn from.
   *
   * Capped at `models/mcmc/model.py::MAX_TRACE_CHAINS` (32) so a
   * high-walker configuration cannot turn every progress message into a
   * large payload; `chain_positions_truncated` says whether that happened,
   * and a view must report it rather than quietly plotting a subset.
   *
   * This field is the model-side extension that the design note recorded as
   * outstanding. It has landed — the note is stale.
   */
  chain_positions: number[][];
  chain_positions_truncated: boolean;
}
/** `primary_metric` is `max_rhat`, and it is null whenever rhat is
 *  non-finite — a real value, not a loading state. */

/* ================================================================== *
 * scenario
 * ================================================================== */

/** `models/scenario/model.py::DEFAULT_GRID` — 6 x 4 x 3 = 72 scenarios. */
export const DEFAULT_GRID = {
  demand: [0.8, 1.0, 1.2, 1.5, 1.8, 2.1],
  capacity: [0.9, 1.0, 1.1, 1.2],
  unit_cost: [0.9, 1.0, 1.1],
} as const;

export const SCENARIO: ModelSpec = {
  name: "scenario",
  label: "Scenario",
  fields: [
    // NOTE: the model reads ONE key, `grid`, as a dict of three lists —
    // not three separate top-level keys. Send { grid: { demand, capacity,
    // unit_cost } }. These render as editable chip-lists of multipliers, the
    // only model whose form is not number inputs.
    { key: "grid.demand", label: "Demand multipliers", kind: "number-list", default: [...DEFAULT_GRID.demand] },
    { key: "grid.capacity", label: "Capacity multipliers", kind: "number-list", default: [...DEFAULT_GRID.capacity] },
    { key: "grid.unit_cost", label: "Unit-cost multipliers", kind: "number-list", default: [...DEFAULT_GRID.unit_cost] },
    { key: "data_days", label: "Days of history", kind: "int", default: 30 },
    { key: "data_seed", label: "Data seed", kind: "int", default: 7 },
    { key: "shortfall_penalty", label: "Shortfall penalty", kind: "float", advanced: true },
    { key: "idle_cost", label: "Idle cost", kind: "float", advanced: true },
    { key: "progress_every", label: "Progress every N scenarios", kind: "int", default: 10, advanced: true },
    { key: "progress_every_s", label: "Progress every N seconds", kind: "float", default: 1.0, advanced: true },
  ],
};

export interface ScenarioProgressPayload {
  scenarios_done: number;
  scenarios_total: number;
  last_scenario: unknown;
  last_outcome: unknown;
}
/** The one model where `percent_complete` is genuinely meaningful the whole
 *  way through. `primary_metric` is `best_objective`. */

/* ================================================================== *
 * streaming_results
 * ================================================================== */

export const STREAMING_RESULTS: ModelSpec = {
  name: "streaming_results",
  label: "Streaming results",
  fields: [
    { key: "days", label: "Days of history", kind: "int", default: 60 },
    { key: "column", label: "Target column", kind: "string", default: "trips" },
    { key: "window", label: "Window size", kind: "int", default: 120 },
    { key: "step", label: "Step", kind: "int", default: 40 },
    { key: "horizon", label: "Horizon", kind: "int", default: 12 },
    { key: "lags", label: "Lag features", kind: "int", default: 24 },
    { key: "seed", label: "Seed", kind: "int", default: 7 },
    { key: "n", label: "Window limit", kind: "int", advanced: true, hint: "Omit for all windows." },
    { key: "series", label: "Series override", kind: "number-list", advanced: true },
  ],
};

export interface StreamingProgressPayload extends ProvenanceFields {
  windows_done: number;
  windows_total: number;
  origin: number;
  /** The model spreads `**self._provenance` into the payload, which
   *  `ProvenanceFields` now names. There may still be EXTRA KEYS beyond all
   *  of these — do not write an exhaustive destructure that assumes not. */
  [key: string]: unknown;
}
/** `primary_metric` is `window_mae`. This is the model that emits `result`
 *  MULTIPLE TIMES per run — each with its own `chunk_index`, `final` false
 *  until the last. APPEND on each result event; never replace. */

/* ================================================================== *
 * annealing
 * ================================================================== */

export const ANNEALING: ModelSpec = {
  name: "annealing",
  label: "Annealing",
  fields: [
    { key: "iterations", label: "Iterations", kind: "int", default: 30000 },
    { key: "n_items", label: "Trips offered", kind: "int", default: 240 },
    {
      key: "capacity_fraction",
      label: "Shift length (fraction)",
      kind: "float",
      default: 0.25,
      hint: "Share of all offered minutes that fit in the shift. At 1.0 you take everything and there is nothing to search.",
    },
    { key: "seed", label: "Search seed", kind: "int", default: 20260823 },
    {
      key: "swap_probability",
      label: "Swap probability",
      kind: "float",
      default: 0.5,
      hint: "Share of moves that swap a trip in for a trip out rather than flipping one. Near a binding capacity, pure flips stall.",
    },
    { key: "data_seed", label: "Data seed", kind: "int", default: 11, advanced: true },
    { key: "baseline_trials", label: "Baseline trials", kind: "int", default: 200, advanced: true },
    // Both derived from the fare distribution when omitted — the defaults are
    // START_TEMPERATURE_FACTOR/END_TEMPERATURE_RATIO applied to the data, not
    // constants, so a sent number is a genuinely different kind of thing.
    { key: "start_temperature", label: "Start temperature", kind: "float", advanced: true, hint: "Omit to derive from the mean fare." },
    { key: "end_temperature", label: "End temperature", kind: "float", advanced: true, hint: "Omit to derive from the start temperature." },
    { key: "progress_every", label: "Progress every N iterations", kind: "int", default: 1000, advanced: true },
    { key: "progress_every_s", label: "Progress every N seconds", kind: "float", default: 1.0, advanced: true },
  ],
};

export interface AnnealingProgressPayload {
  iteration: number;
  iterations_total: number;
  temperature: number;
  /** Value minus the overweight penalty — this is the number that goes DOWN
   *  on purpose. See the note below before charting it. */
  current_objective: number;
  current_value: number;
  current_weight: number;
  capacity: number;
  /** `current_weight <= capacity`. The search crosses out of feasibility
   *  deliberately; false is the algorithm working, not an error state. */
  feasible: boolean;
  acceptance_rate: number;
  accepted_total: number;
  /** Of the BEST selection, not the current one — the only payload key that
   *  describes the incumbent rather than the walk. Easy to mis-pair with
   *  `current_value`; they are not from the same solution. */
  items_selected: number;
}
/** `percent_complete` IS populated: 100*iterations_run/iterations, and
 *  honestly so — the iteration count is the whole plan up front.
 *  `primary_metric` is `best_fare`, the BEST value so far, and is monotonic.
 *
 *  This is the one model whose payload is deliberately non-monotonic, and the
 *  chart has to say so: plot `current_objective` AGAINST `primary_metric`.
 *  A view that shows only the current objective makes a working search look
 *  like it is failing early on, which is exactly the reading the model's
 *  two-number split exists to prevent.
 *
 *  Also: `progress_every` doubles as the cancellation-check interval — the
 *  loop only tests `should_cancel` every N iterations — so a cancel can take
 *  up to that many iterations to land. Do not show "cancelling" as hung. */

/* ================================================================== *
 * bayesian_ab
 * ================================================================== */

/** `models/bayesian_ab/model.py::COMPARISONS`. The constructor RAISES on
 *  anything else — the only model here that validates its own config, so a
 *  typo is a FAILED run rather than a surprising default. Render as a choice,
 *  never a free-text field. */
export const BAYESIAN_AB_COMPARISONS = ["weekend_fare", "long_trip_speed"] as const;
export type BayesianAbComparison = (typeof BAYESIAN_AB_COMPARISONS)[number];

/** `models/bayesian_ab/model.py::STAGES`, in order. Five named steps, not a
 *  curve — the progress bar for this model is a stepper. */
export const BAYESIAN_AB_STAGES = [
  "posteriors",
  "comparison",
  "expected_loss",
  "lift_interval",
  "decision",
] as const;

export const BAYESIAN_AB: ModelSpec = {
  name: "bayesian_ab",
  label: "Bayesian A/B",
  fields: [
    {
      key: "comparison",
      label: "Comparison",
      kind: "string",
      default: BAYESIAN_AB_COMPARISONS[0],
      hint: "weekend_fare (weekday vs weekend hours) or long_trip_speed (short vs long trips). Anything else fails the run.",
    },
    // These two are comparison-specific: `hours` is read only on the
    // weekend_fare path, `rows` only on long_trip_speed. Both are always
    // read from cfg, so sending the irrelevant one is harmless — but showing
    // it is misleading. Gate the field on `comparison`.
    {
      key: "hours",
      label: "Hours of history",
      kind: "int",
      default: 1440,
      hint: "weekend_fare only. Quantised to whole days — the loader takes max(1, hours // 24), so 1440 and 1463 are both 60 days.",
    },
    { key: "rows", label: "Trips sampled", kind: "int", default: 2000, hint: "long_trip_speed only." },
    { key: "data_seed", label: "Data seed", kind: "int", default: 7 },
    {
      key: "decision_threshold",
      label: "Decision threshold",
      kind: "float",
      default: 0.95,
      hint: "P(B>A) an arm must clear to lead.",
    },
    {
      key: "loss_tolerance",
      label: "Loss tolerance",
      kind: "float",
      default: 0.002,
      hint: "In units of the rate. The leader's expected loss must also be under this for the call to be conclusive.",
    },
    // Must both be > 0; the constructor raises otherwise. Jeffreys' (0.5,
    // 0.5) is supported and costs one integral its closed form.
    { key: "prior_alpha", label: "Prior alpha", kind: "float", default: 1.0, advanced: true },
    { key: "prior_beta", label: "Prior beta", kind: "float", default: 1.0, advanced: true },
    { key: "credible_mass", label: "Credible mass", kind: "float", default: 0.95, advanced: true },
    {
      key: "fare_threshold",
      label: "Fare threshold",
      kind: "float",
      advanced: true,
      hint: "weekend_fare only. Omit for the pooled median — honest, but derived from the data being tested, which couples the two arms.",
    },
    { key: "speed_threshold_mph", label: "Speed threshold (mph)", kind: "float", default: 12.0, advanced: true, hint: "long_trip_speed only." },
    { key: "split_miles", label: "Long-trip cutoff (miles)", kind: "float", default: 2.0, advanced: true, hint: "long_trip_speed only." },
    // Two structured escape hatches. `arms` is a list of exactly two
    // {label, trials, successes} objects and bypasses the data path
    // entirely; `data` is a list of raw row objects for the chosen
    // comparison. Both are JSON, both are typed `string` here for the same
    // reason mcmc's `data` is — this descriptor set drives a form, and there
    // is no object kind. Parse before sending; a malformed `arms` raises.
    { key: "arms", label: "Arms override", kind: "string", advanced: true, hint: "JSON: exactly two {label, trials, successes}." },
    { key: "data", label: "Data override", kind: "string", advanced: true },
  ],
};

export interface BayesianAbArmSummary {
  role: "A" | "B";
  label: string;
  trials: number;
  successes: number;
  /**
   * Null before the `posteriors` stage has run for this arm. In practice
   * that means the RESULT ROWS only: `_progress` is called after
   * `stages_done = index`, i.e. after `_stage_posteriors`, so the arms on a
   * progress message are already fitted from stage 1 onward. Kept nullable
   * because the result path is real and because a stage-0 message would have
   * it null.
   *
   * With these two and the prior, a view can redraw both densities with no
   * round trip.
   */
  posterior_alpha: number | null;
  posterior_beta: number | null;
  posterior_mean: number | null;
}

export interface BayesianAbProgressPayload {
  stage: string;
  /**
   * ONE-BASED, and a count of COMPLETED stages rather than an array index.
   * `models/bayesian_ab/model.py` iterates `enumerate(STAGES, start=1)` and
   * emits after the stage body has run, so the first message carries 1 and
   * the last carries 5.
   *
   * Spelled out because it was previously left implicit and two readers drew
   * opposite conclusions from the same file: `percent_complete` documented as
   * 20/40/60/80/100 only works if this is 1-based, while
   * `BAYESIAN_AB_STAGES[stage_index]` only works if it is 0-based. It is
   * 1-based; the array lookup needs `stage_index - 1`.
   */
  stage_index: number;
  stages_total: number;
  /** Literally the string "stages". A rendering instruction the model puts in
   *  the payload on purpose: do not join these points with a line. No other
   *  model emits this key. */
  progress_shape: "stages";
  comparison: string;
  /** Prose — what "success" means for this comparison, threshold included.
   *  Show it; it is the only place the decision table's units are stated. */
  outcome: string;
  prior: { alpha: number; beta: number };
  credible_mass: number;
  arms: BayesianAbArmSummary[];
  /** The five below are ADDED to the payload as their stages complete — they
   *  are genuinely absent from earlier messages, not null in them. Optional
   *  in the TS sense, so `in` / `!== undefined`, not a null check. */
  prob_b_beats_a?: number;
  expected_loss?: { A: number; B: number };
  lift?: Record<string, unknown>;
  decision?: string;
  conclusive?: boolean;
}
/** `percent_complete` is 100*index/5 — 20, 40, 60, 80, 100, never 0, and it
 *  is a step counter rather than a curve. `primary_metric` is
 *  `prob_b_beats_a`, null until the `comparison` stage computes it, and it is
 *  a probability in [0,1] — not an error or a gap, so lower is not better.
 *
 *  THE THING THAT WILL BITE: this model is closed-form. The whole run is
 *  milliseconds, so a live client will routinely see the terminal status with
 *  NO progress messages in between, or all five at once. Every other model
 *  here gives the UI time to draw an intermediate state. Do not write a view
 *  that only reaches its populated form by way of a progress event —
 *  backfill from the result, and treat a stage bar that jumps straight to
 *  done as the normal case.
 *
 *  `decision` is one of the two arm LABELS (e.g. "weekend_hours") or the
 *  string "inconclusive" — not "A"/"B", and not a boolean.
 *
 *  CORRECTED: this said "an arm can lead without being conclusive". True of
 *  the underlying state, but the payload collapses it. `_stage_decision`
 *  sets `decision` to the leader's label only when `conclusive`, so a
 *  leading-but-lossy arm reports "inconclusive" and `decision !=
 *  "inconclusive"` implies `conclusive === true`. Worse, neither
 *  `decision_threshold` nor `loss_tolerance` reaches the payload OR the
 *  result rows, so a view cannot recover which arm was leading in that case
 *  — show P(B>A), both expected losses and the lift instead of inventing a
 *  leader. */

/* ================================================================== *
 * neural_net
 * ================================================================== */

/** `models/neural_net/model.py::CLASS_LABELS`, in the network's own class-index
 *  order, so a result row's `class_index` joins to this without a lookup. */
export const NEURAL_NET_CLASS_LABELS = ["fast", "typical", "congested"] as const;

/**
 * `models/neural_net/model.py::FEATURE_NAMES` — the network's input layer, in
 * order. Four basis transforms of ONE column, `trip_distance`; the width of
 * the input layer is therefore a fixed property of the model, unlike the
 * hidden widths, which are config and never reach the payload.
 *
 * Mirrored here because a view that draws the architecture would otherwise
 * hardcode the number 4 with a comment pointing at Python, which is the same
 * drift this file exists to prevent — just smaller.
 */
export const NEURAL_NET_FEATURE_NAMES = [
  "distance",
  "log1p_distance",
  "sqrt_distance",
  "inv_distance",
] as const;

export const NEURAL_NET: ModelSpec = {
  name: "neural_net",
  label: "Neural net",
  fields: [
    { key: "limit", label: "Rows sampled", kind: "int", default: 4000 },
    { key: "epochs", label: "Epochs", kind: "int", default: 12 },
    { key: "batch_size", label: "Batch size", kind: "int", default: 128 },
    {
      key: "hidden",
      label: "Hidden layers",
      kind: "number-list",
      default: [32, 16],
      hint: "One width per layer.",
    },
    { key: "lr", label: "Learning rate", kind: "float", default: 0.01 },
    { key: "seed", label: "Seed", kind: "int", default: 17 },
    { key: "lr_decay", label: "LR decay / epoch", kind: "float", default: 0.9, advanced: true },
    { key: "val_fraction", label: "Validation fraction", kind: "float", default: 0.25, advanced: true },
    {
      key: "cut_quantiles",
      label: "Class cut quantiles",
      kind: "number-list",
      default: [0.55, 0.85],
      advanced: true,
      hint: "Where fast/typical/congested split, on the TRAINING quantiles only. Moving these changes the class balance and therefore the baseline the accuracy has to beat.",
    },
    {
      key: "batch_updates_per_epoch",
      label: "Batch samples / epoch",
      kind: "int",
      default: 2,
      advanced: true,
      hint: "Intra-epoch progress messages. Each one re-evaluates validation, so this is a real cost, not just chatter.",
    },
    {
      key: "device",
      label: "Torch device",
      kind: "string",
      advanced: true,
      hint: "Omit to auto-select — cuda when available, else cpu.",
    },
  ],
};

export interface NeuralNetProgressPayload {
  /** "epoch" or "batch". BOTH levels arrive interleaved on ONE stream, so a
   *  chart keyed on `epoch` alone gets several points per x. Key on the
   *  message's `seq`, or filter to `level === "epoch"`. */
  level: string;
  epoch: number;
  epochs_total: number;
  batch: number;
  batches_per_epoch: number;
  train_loss: number;
  /** Always a number: the batch-level path re-evaluates validation rather
   *  than carrying a stale figure, so `primary_metric` is never a repeat. */
  val_loss: number;
  macro_f1: number;
  grad_norm: number;
  learning_rate: number;
  /** Null through the FIRST epoch's batch samples — best-checkpoint tracking
   *  only updates at epoch end. Null means "no epoch has finished", not
   *  "zero". */
  best_val_accuracy: number | null;
  /** What predicting the majority class alone would score. The classes are
   *  ~55/30/15 on purpose, so accuracy without this next to it flatters the
   *  model; render them on the same axis. */
  baseline_accuracy: number;
  /** "cpu" / "cuda" / whatever `device` was overridden to. The one field that
   *  keeps a CPU run and a GPU run distinguishable after the fact. */
  device: string;
  data_synthetic: boolean | null;
}
/** `percent_complete` IS populated and is monotonic across both levels —
 *  100*steps/total_steps over batch steps, not epochs, so it advances between
 *  epoch boundaries. `primary_metric` is `val_accuracy`, bounded 0..1.
 *
 *  Unlike `forecasting`, `val_loss` IS in the payload here, and the metric is
 *  an accuracy rather than a loss — so higher is better on this one chart and
 *  worse on that one. Do not share a "metric went up = good" component.
 *
 *  Cheap by design: a few thousand rows, ~a second of CPU training. Like
 *  bayesian_ab, expect few progress messages per run. */

/* ================================================================== *
 * Registry
 * ================================================================== */

/** All nine. Cross-checked against `[tool.dbx-leaning.models]` in
 *  `pyproject.toml` and the nine `resources/model_*.job.yml` — all three sets
 *  agree today. They are three hand-maintained lists with no test tying them
 *  together, so re-check rather than trusting this comment. */
/* ================================================================== *
 * ortools_jobshop
 * ================================================================== */

/** `models/ortools_jobshop/instance.py::MAX_JOBS`. `build_instance` RAISES
 *  above this, so a form should refuse rather than let a run fail. The cap is
 *  the job task's hour, not a solver limit — CP-SAT has no size cap, which is
 *  the whole point of this model against the two Gurobi ones. */
export const JOBSHOP_MAX_JOBS = 400;

export const ORTOOLS_JOBSHOP: ModelSpec = {
  name: "ortools_jobshop",
  label: "Job shop (CP-SAT)",
  fields: [
    {
      key: "max_jobs",
      label: "Jobs",
      kind: "int",
      default: 60,
      hint: `Franchise-day product batches to schedule. Refused above ${JOBSHOP_MAX_JOBS}.`,
    },
    {
      key: "max_time_in_seconds",
      label: "Time limit (s)",
      kind: "float",
      default: 60,
      hint: "Also the denominator for percent complete — see the payload note.",
    },
    { key: "seed", label: "Seed", kind: "int", default: 20260824 },
    {
      key: "use_sample_data",
      label: "Use sample-catalog orders",
      kind: "bool",
      default: true,
      hint: "On = real bakehouse transactions. Off = the deterministic generator.",
    },
    {
      key: "workers",
      label: "Solver workers",
      kind: "int",
      default: 0,
      advanced: true,
      hint: "0 means CP-SAT decides from available cores. Pin to 1 for a reproducible incumbent sequence.",
    },
    {
      key: "deadline_minutes",
      label: "Deadline (min)",
      // INT, not float. The model reads it through `_optional_int`, so a
      // float is silently truncated — 90.5 becomes 90 and nobody notices.
      kind: "int",
      advanced: true,
      hint: "Omit for none. This is the ONLY way this model reaches INFEASIBLE — an open-horizon job shop can always run its jobs end to end.",
    },
    { key: "progress_every_s", label: "Progress interval (s)", kind: "float", default: 2, advanced: true },
    {
      key: "solver_log",
      label: "Capture solver log",
      kind: "bool",
      default: true,
      advanced: true,
      hint: "Off costs cancellation latency, not just log volume: the log callback is what polls for cancel on a stalled search.",
    },
  ],
};

/**
 * From `models/ortools_jobshop/model.py::_emit_progress`.
 *
 * `incumbent` and `best_bound` are nullable for the same reason as the Gurobi
 * models', though by a different mechanism — CP-SAT's pre-search bound is a
 * real infinity, nulled by `_finite`. `conflicts` and `branches` are ABSENT,
 * not null, when the solver did not hand them over.
 */
export interface JobshopProgressPayload {
  incumbent: number | null;
  best_bound: number | null;
  gap: number | null;
  solutions_found: number;
  wall_time: number;
  n_jobs: number;
  n_machines: number;
  n_operations: number;
  /** Literally "elapsed_solver_time_against_time_limit". The model names its
   *  own basis in the record because the envelope has no label field for
   *  percent_complete, and this is a TIME fraction, not a search fraction. */
  percent_complete_basis: string;
  final: boolean;
  conflicts?: number;
  branches?: number;
  /** Only on the final sample. CP-SAT's own name: OPTIMAL / FEASIBLE /
   *  INFEASIBLE / MODEL_INVALID / UNKNOWN — not a `RunStatus`. */
  solver_status?: string;
}
/** `primary_metric_label` is **`relative_gap`**, deliberately not `mip_gap`:
 *  same formula as the Gurobi driver, but this is not a MIP. `percent_complete`
 *  is honest here and null only when no time limit is set. */

/* ================================================================== *
 * panel_fit
 * ================================================================== */

/** `models/panel_fit/model.py`. A closed set on purpose — a UI groups by
 *  these, so they are not free text. */
export const PANEL_FIT_FAILURE_REASONS = [
  "too_few_observations",
  "zero_predictor_variance",
  "singular_design",
  "non_finite_result",
] as const;

/** `GROUP_STATUSES`. Two values, so "how many failed" is one GROUP BY without
 *  enumerating the reason set. */
export const PANEL_FIT_GROUP_STATUSES = ["fitted", "failed"] as const;

export const PANEL_FIT: ModelSpec = {
  name: "panel_fit",
  label: "Panel fit",
  fields: [
    {
      key: "table",
      label: "Panel table",
      kind: "string",
      default: "main.dbx_leaning.owid_country_year",
      hint: "OWID-shaped country x year data. This table does NOT exist yet, so the default run uses the synthetic panel and says so in its provenance.",
    },
    { key: "response_column", label: "Response", kind: "string", default: "life_expectancy" },
    { key: "predictor_column", label: "Predictor", kind: "string", default: "year", hint: "Defaults to the period column." },
    { key: "group_column", label: "Group by", kind: "string", default: "entity" },
    {
      key: "label_column",
      label: "Group label",
      kind: "string",
      default: "code",
      hint: "Feeds `group_label` — the display name a view shows for each group.",
    },
    { key: "period_column", label: "Period", kind: "string", default: "year" },
    { key: "degree", label: "Polynomial degree", kind: "int", default: 1, hint: "2 fits a curve — the Kuznets shape." },
    { key: "limit", label: "Row limit", kind: "int", default: 20000 },
    { key: "seed", label: "Seed", kind: "int", default: 24 },
    {
      key: "min_observations",
      label: "Min observations / group",
      kind: "int",
      advanced: true,
      hint: "Omit for degree + 2, the fewest points a fit of that degree can use.",
    },
    { key: "max_groups", label: "Max groups", kind: "int", advanced: true, hint: "Omit for all of them." },
    { key: "chunk_size", label: "Result chunk size", kind: "int", default: 12, advanced: true },
    { key: "progress_every", label: "Progress every N groups", kind: "int", default: 1, advanced: true },
    { key: "failure_log_limit", label: "Failure log limit", kind: "int", default: 12, advanced: true },
  ],
};

/**
 * From `models/panel_fit/model.py`.
 *
 * The interesting field is the fitted/failed split, on EVERY progress message.
 * No other model on this platform reports per-unit outcomes, and a client has
 * to be able to tell a healthy run from one quietly failing a third of its
 * groups without waiting for the results.
 *
 * `groups_fitted + groups_failed === groups_done`, always.
 */
export interface PanelFitProgressPayload extends ProvenanceFields {
  groups_done: number;
  groups_total: number;
  groups_fitted: number;
  groups_failed: number;
  /** Reason -> count, keyed by `PANEL_FIT_FAILURE_REASONS`. */
  failure_counts: Record<string, number>;
  group_key: string;
  group_label: string;
  /** One of `PANEL_FIT_GROUP_STATUSES`. */
  group_status: string;
  group_failure_reason: string | null;
  group_r_squared: number | null;
  n_observations: number;
  /** Rows the group HAD, against the ones that survived the null and
   *  non-finite drop. The difference between "this unit is small" and "this
   *  unit did not report", which is the first question about any failure. */
  rows_seen: number;
  metric_higher_is_better: boolean;
  degree: number;
  chunks_emitted: number;
}
/** `primary_metric` is the MEDIAN r-squared across fitted groups — median so
 *  one pathological group cannot move a 180-group headline — and is null until
 *  something has been fitted, which is legal. Higher is better here, unlike
 *  forecasting. `percent_complete` is groups done over groups total, with no
 *  estimation: the denominator is known before the first fit.
 *
 *  This model CHUNKS its results (`chunk_size` groups per chunk), so
 *  `streaming_results` is no longer the only one that does. */

export const MODEL_SPECS: readonly ModelSpec[] = [
  GUROBI_SCHEDULING,
  GUROBI_ROUTING,
  FORECASTING,
  MCMC,
  SCENARIO,
  STREAMING_RESULTS,
  ANNEALING,
  BAYESIAN_AB,
  NEURAL_NET,
  ORTOOLS_JOBSHOP,
  PANEL_FIT,
];

export type ModelName = (typeof MODEL_SPECS)[number]["name"];

/** Which models are ACTUALLY triggerable comes from `GET /api/models`, which
 *  derives it from the configured job map — a model can exist in `models/`
 *  with no job behind it, and cannot be run. Cross-reference this static list
 *  against that response; render the difference rather than hiding it. */
