/**
 * Synthetic `RunSnapshot`s for the model-view gallery.
 *
 * There is no Databricks workspace to point at, so this file is the only
 * source of envelope traffic the nine per-model views can be built and
 * reviewed against. That makes it a *contract mirror*, not a mock: every
 * payload here is written against the interface declared for that model in
 * `@/lib/models`, and where the real model's shape is ambiguous the
 * ambiguity is written down at the point it bites rather than smoothed over.
 *
 * Four rules it holds to, because nine other test suites are about to be
 * built on top of it:
 *
 *  1. **Deterministic.** A seeded PRNG, seeded from `model|fixture` only —
 *     never `Math.random()`, never `Date.now()`. Two calls return
 *     deep-equal data, so a screenshot diff means something, and the same
 *     entry index yields the same numbers in every lifecycle state, so
 *     RUNNING genuinely looks like SUCCEEDED caught early.
 *
 *  2. **One seq counter per run, shared by all four message types.** That is
 *     the real contract (`envelope.ts`), and it is the thing a view that
 *     keys a chart on `seq` depends on. Assigned here in emission order.
 *
 *  3. **Built through the real `RunStore`.** The derived fields —
 *     `latestProgress`, `status`, `terminal`, `lastSeq`, the drop counters —
 *     are computed by the same code the app runs, not hand-written here. A
 *     hand-built snapshot would be free to be subtly impossible.
 *
 *  4. **Faithful over uniform.** `bayesian_ab` has exactly five stages, so
 *     its "dense" fixture is still five messages. A model's own totals scale
 *     with the message count (`epochs_total` = number of epoch messages)
 *     rather than emitting 2000 messages for a 40-epoch run, which no view
 *     could read sensibly and no run could produce.
 */

import type {
  LogLevel,
  LogMessage,
  Message,
  ProgressMessage,
  ResultMessage,
  RunStatus,
  StatusMessage,
  UiRunState,
} from "@/lib/envelope";
import {
  BAYESIAN_AB_STAGES,
  DEFAULT_GRID,
  NEURAL_NET_CLASS_LABELS,
} from "@/lib/models";
import type { ConnectionState } from "@/transport/protocol";
import { RunStore, type Gap, type RunSnapshot } from "@/transport/runStore";

/* ================================================================== *
 * Seeded randomness
 * ================================================================== */

type Rng = () => number;

/** mulberry32 — 32 bits of state, no dependencies, and the same sequence in
 *  node and the browser, which a screenshot diff needs. */
function makeRng(seed: number): Rng {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function hashSeed(text: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i += 1) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

/** Fixture numbers are read off a screen and diffed between runs; float
 *  noise in the fourteenth decimal place is pure distraction. */
function round(x: number, dp = 3): number {
  const f = 10 ** dp;
  return Math.round(x * f) / f;
}

/* ================================================================== *
 * The named fixtures
 * ================================================================== */

export type FixtureName =
  | "empty"
  | "sparse"
  | "typical"
  | "dense"
  | "null-heavy"
  | "chunked"
  | "gappy";

export const FIXTURE_NAMES = [
  "typical",
  "empty",
  "sparse",
  "dense",
  "null-heavy",
  "chunked",
  "gappy",
] as const satisfies readonly FixtureName[];

/** Why each fixture exists. Rendered in the gallery — a reviewer who does not
 *  know why `empty` is a real state cannot tell a broken view from a correct
 *  one looking at it. */
export const FIXTURE_NOTES: Record<FixtureName, string> = {
  typical: "What a default-config run of this model actually emits. The baseline everything else is a deviation from.",
  empty: "Zero messages of any type. Real: bayesian_ab is closed-form and finishes in milliseconds, so a client routinely learns the terminal status from GET /api/runs and observes no progress at all. `status` is set via markTerminal; every array is empty and `lastSeq` is null.",
  sparse: "Eight progress messages for the whole run. Real: annealing emits ~30, neural_net ~36. A view that needs a hundred points to look right fails here.",
  dense: "Two thousand progress points. Real: mcmc at a low progress_every. A view that hands every point to Recharts is visibly slow here, and that is the point of the fixture.",
  "null-heavy": "percent_complete null and primary_metric null on every message. Real and permanent for gurobi_scheduling, transient for mcmc/scenario/streaming. Null is a value, never a loading state — an indeterminate treatment, never a 0% bar.",
  chunked: "Five result messages with rising chunk_index, final:false until the last. Only streaming_results does this for real; every view gets it because appending rather than replacing is cheap to get wrong.",
  gappy: "A hole in the seq stream, plus the client_visible:false logs that backfill will not return. This gap never closes — a view must mark it, not silently join across it.",
};

interface FixturePlan {
  /** Null means "this model's natural volume". */
  progressCount: number | null;
  logCount: number;
  resultChunks: number;
  forceNullPercent: boolean;
  forceNullMetric: boolean;
  /** Burn a run of seq numbers mid-stream — the un-closeable gap. */
  gapSize: number;
}

const FIXTURE_PLANS: Record<FixtureName, FixturePlan> = {
  typical: { progressCount: null, logCount: 40, resultChunks: 1, forceNullPercent: false, forceNullMetric: false, gapSize: 0 },
  empty: { progressCount: 0, logCount: 0, resultChunks: 0, forceNullPercent: false, forceNullMetric: false, gapSize: 0 },
  sparse: { progressCount: 8, logCount: 6, resultChunks: 1, forceNullPercent: false, forceNullMetric: false, gapSize: 0 },
  dense: { progressCount: 2000, logCount: 1200, resultChunks: 1, forceNullPercent: false, forceNullMetric: false, gapSize: 0 },
  "null-heavy": { progressCount: null, logCount: 12, resultChunks: 1, forceNullPercent: true, forceNullMetric: true, gapSize: 0 },
  chunked: { progressCount: null, logCount: 16, resultChunks: 5, forceNullPercent: false, forceNullMetric: false, gapSize: 0 },
  gappy: { progressCount: null, logCount: 24, resultChunks: 1, forceNullPercent: false, forceNullMetric: false, gapSize: 6 },
};

/* ================================================================== *
 * Lifecycle recipes
 * ================================================================== */

/** How much of a planned run each lifecycle state has got through, and what
 *  it did with its results. */
interface StateRecipe {
  /** Fraction of the planned message timeline that has arrived. */
  fraction: number;
  /** Non-terminal statuses seen on the stream, in order. */
  live: readonly RunStatus[];
  terminal: RunStatus | null;
  /**
   * - `planned`  keep the chunks the timeline scheduled
   * - `none`     the run never reached the write
   * - `incumbent` one result holding whatever was best when it stopped
   * - `empty`    one result with row_count 0 — "got there, wrote nothing",
   *              which is exactly what row_count exists to distinguish
   */
  results: "planned" | "none" | "incumbent" | "empty";
  connection: ConnectionState;
  closing?: { level: LogLevel; text: string };
}

const STATE_RECIPES: Record<UiRunState, StateRecipe> = {
  // Client-only. The 202 has come back and nothing else has: no status, no
  // progress, no logs. Any view that only reaches a drawable form via a
  // progress message shows its worst self here.
  STARTING: { fraction: 0, live: [], terminal: null, results: "none", connection: "connecting" },
  QUEUED: { fraction: 0, live: ["QUEUED"], terminal: null, results: "none", connection: "open" },
  RUNNING: { fraction: 0.6, live: ["QUEUED", "RUNNING"], terminal: null, results: "planned", connection: "open" },
  SUCCEEDED: { fraction: 1, live: ["QUEUED", "RUNNING"], terminal: "SUCCEEDED", results: "planned", connection: "idle" },
  FAILED: {
    fraction: 0.45,
    live: ["QUEUED", "RUNNING"],
    terminal: "FAILED",
    results: "none",
    connection: "idle",
    closing: { level: "ERROR", text: "unhandled exception in model.run(); see driver logs" },
  },
  CANCELLED: {
    fraction: 0.7,
    live: ["QUEUED", "RUNNING"],
    terminal: "CANCELLED",
    // Cancelled keeps its incumbent — CLAUDE.md is explicit that a result is
    // written whenever execution reaches that point, terminal status or not.
    results: "incumbent",
    connection: "idle",
    closing: { level: "WARNING", text: "cancel requested; flushing incumbent before exit" },
  },
  // Proven infeasible stops early; it does not grind to 100%.
  INFEASIBLE: { fraction: 0.85, live: ["QUEUED", "RUNNING"], terminal: "INFEASIBLE", results: "empty", connection: "idle" },
};

/* ================================================================== *
 * Per-model scripts
 * ================================================================== */

interface ProgressPoint {
  percent_complete: number | null;
  primary_metric: number | null;
  payload: Record<string, unknown>;
}

interface LogTemplate {
  level: LogLevel;
  phase: string;
  text: string | ((i: number) => string);
}

interface ModelScript {
  /** Progress messages a default-config run emits. Derived from each model's
   *  own defaults in `models.ts` (mcmc: 3000 draws / progress_every 200 = 15;
   *  annealing: 30000 / 1000 = 30; neural_net: 12 epochs x 3 = 36). */
  naturalCount: number;
  /** Hard ceiling. bayesian_ab has five stages and "dense" must not invent a
   *  sixth — an impossible fixture teaches a view the wrong lesson. */
  maxCount?: number;
  metricLabel: string;
  /** Wall clock for the whole run; sets `elapsed_seconds` and `ts`. */
  durationS: number;
  /**
   * A factory, so a script can carry running state (best-so-far, cumulative
   * counters) down the series instead of recomputing it at every index.
   *
   * `nullish` is the `null-heavy` fixture asking for every NULLABLE PAYLOAD
   * field to be null as well — `best_val_accuracy`, the bayesian arms'
   * posteriors, `data_synthetic`. Forcing `percent_complete` and
   * `primary_metric` to null is done centrally; only the payload knows which
   * of its own fields are legally null, so only the script can do that part.
   */
  progress: (count: number, rng: Rng, nullish: boolean) => (i: number) => ProgressPoint;
  logs: readonly LogTemplate[];
  /** Open set in practice — "model" (default), "job", "gurobi". */
  logSource: string;
  preview: (rng: Rng, chunk: number) => Array<Record<string, unknown>>;
  /** Rows written durably by a complete run. */
  rowCount: number;
  fetchHint: (runId: string) => Record<string, unknown>;
  detail: Partial<Record<RunStatus, string>>;
}

const hint =
  (table: string) =>
  (runId: string): Record<string, unknown> => ({
    table,
    key_columns: ["run_id"],
    run_id: runId,
  });

/* ---------------------------------------------------------------- *
 * gurobi_scheduling
 * ---------------------------------------------------------------- */

/**
 * `percent_complete` is ALWAYS null here — a MIP has no honest completion
 * fraction — and `primary_metric` is the MIP gap. Both the bound and the
 * incumbent move monotonically (bound up, incumbent down) because that is
 * what branch and bound guarantees; the jitter goes on the node counts,
 * where it belongs.
 */
const GUROBI_SCHEDULING_SCRIPT: ModelScript = {
  naturalCount: 45,
  metricLabel: "mip_gap",
  durationS: 180,
  progress: (count, rng) => (i) => {
    const p = (i + 1) / count;
    const bound = 4210 + 590 * p;
    const incumbent = 6380 - 1460 * p;
    const explored = Math.round(140 * (i + 1) * (1 + 0.4 * rng()));
    return {
      percent_complete: null,
      primary_metric: round((incumbent - bound) / incumbent, 5),
      payload: {
        best_bound: round(bound, 2),
        incumbent: round(incumbent, 2),
        nodes_explored: explored,
        nodes_remaining: Math.round(explored * (0.8 - 0.6 * p) + 12 * rng()),
        solution_count: 1 + Math.floor(p * 6),
      },
    };
  },
  logs: [
    { level: "INFO", phase: "input", text: "loaded 336 hourly demand points from samples.nyctaxi.trips" },
    { level: "INFO", phase: "build", text: "model: 1680 binaries, 1204 constraints" },
    { level: "DEBUG", phase: "solve", text: (i) => `presolve pass ${i % 4} removed ${12 + (i % 7)} rows` },
    { level: "INFO", phase: "solve", text: (i) => `new incumbent at node ${1400 + i * 37}` },
    { level: "WARNING", phase: "solve", text: "degenerate LP relaxation; switching to dual simplex" },
  ],
  logSource: "gurobi",
  preview: (rng, chunk) =>
    Array.from({ length: 6 }, (_, k) => ({
      staff_id: `S${String(chunk * 6 + k + 1).padStart(3, "0")}`,
      day: (chunk * 6 + k) % 14,
      shift: ["early", "mid", "late"][(chunk + k) % 3] ?? "early",
      hours: round(6 + 3 * rng(), 1),
    })),
  rowCount: 280,
  fetchHint: hint("main.dbx_leaning.gurobi_scheduling_results"),
  detail: {
    RUNNING: "solver started; restricted licence, 1680 binaries",
    SUCCEEDED: "optimal within 0.4% gap",
    FAILED: "gurobipy raised GurobiError: model too large for restricted licence",
    CANCELLED: "cancelled by user; incumbent retained",
    INFEASIBLE: "no schedule satisfies max_shifts_per_staff=10 at this demand",
  },
};

/* ---------------------------------------------------------------- *
 * gurobi_routing — same payload interface, different arithmetic
 * ---------------------------------------------------------------- */

const GUROBI_ROUTING_SCRIPT: ModelScript = {
  naturalCount: 32,
  metricLabel: "mip_gap",
  durationS: 120,
  progress: (count, rng) => (i) => {
    const p = (i + 1) / count;
    const bound = 782 + 128 * p;
    const incumbent = 1174 - 240 * p;
    const explored = Math.round(90 * (i + 1) * (1 + 0.5 * rng()));
    return {
      percent_complete: null,
      primary_metric: round((incumbent - bound) / incumbent, 5),
      payload: {
        best_bound: round(bound, 2),
        incumbent: round(incumbent, 2),
        nodes_explored: explored,
        nodes_remaining: Math.round(explored * (0.7 - 0.5 * p) + 9 * rng()),
        solution_count: 1 + Math.floor(p * 5),
      },
    };
  },
  logs: [
    { level: "INFO", phase: "input", text: "24 stops, 3 vehicles, radii from samples trips" },
    { level: "INFO", phase: "build", text: "edge formulation: 300 arc vars + 24 node vars" },
    // cuts_added / separation_calls reach the logs and the results but NEVER
    // a progress payload (models.ts). Putting them here and nowhere else is
    // deliberate: a live cut counter would have nothing to read.
    { level: "DEBUG", phase: "solve", text: (i) => `separation call ${i + 1}: ${1 + (i % 3)} rounded-capacity cuts added` },
    { level: "INFO", phase: "solve", text: (i) => `incumbent tour cost ${round(1174 - i * 3.2, 1)}` },
  ],
  logSource: "gurobi",
  preview: (rng, chunk) =>
    Array.from({ length: 6 }, (_, k) => ({
      vehicle: (chunk + k) % 3,
      stop_seq: k,
      stop_id: `T${String((chunk * 6 + k) % 24).padStart(2, "0")}`,
      arrival_min: round(18 * k + 40 * rng(), 1),
      service_min: round(4 + 6 * rng(), 1),
    })),
  rowCount: 27,
  fetchHint: hint("main.dbx_leaning.gurobi_routing_results"),
  detail: {
    RUNNING: "solving; 120s default time limit",
    SUCCEEDED: "optimal tour found, 3 vehicles used",
    FAILED: "instance build raised: stop_count 61 exceeds MAX_STOPS 55",
    CANCELLED: "cancelled by user; best tour retained",
    // models.ts is explicit that this is a correct answer to a badly posed
    // question, and that the numbers behind it belong in the UI.
    INFEASIBLE: "3 vehicles cannot cover 812 service minutes within the shift; needs 5",
  },
};

/* ---------------------------------------------------------------- *
 * forecasting
 * ---------------------------------------------------------------- */

/** `val_loss` is NOT in the payload — it is `primary_metric`. The chart plots
 *  `payload.train_loss` against the metric, not two payload keys. */
const FORECASTING_SCRIPT: ModelScript = {
  naturalCount: 40,
  metricLabel: "val_loss",
  durationS: 34,
  progress: (count, rng, nullish) => {
    let bestVal = Number.POSITIVE_INFINITY;
    return (i) => {
      const p = (i + 1) / count;
      const train = 0.92 * Math.exp(-2.6 * p) + 0.058;
      const val = train * 1.14 + 0.02 * (rng() - 0.45);
      bestVal = Math.min(bestVal, val);
      return {
        percent_complete: round(100 * ((i + 1) / count), 2),
        primary_metric: round(val, 5),
        payload: {
          epoch: i,
          epochs_total: count,
          train_loss: round(train, 5),
          best_val_loss: round(bestVal, 5),
          learning_rate: round(0.01 * 0.95 ** i, 6),
          // Nullable on the wire: the loader does not always know.
          data_synthetic: nullish ? null : false,
        },
      };
    };
  },
  logs: [
    { level: "INFO", phase: "input", text: "60 days of hourly trips; 1440 rows, 0 gaps" },
    { level: "INFO", phase: "build", text: "24 lag features, horizon 48" },
    { level: "DEBUG", phase: "run", text: (i) => `epoch ${i} in ${round(0.41 + (i % 5) * 0.02, 2)}s` },
    { level: "INFO", phase: "run", text: (i) => `checkpoint: best val_loss improved at epoch ${i}` },
  ],
  logSource: "model",
  preview: (rng, chunk) =>
    Array.from({ length: 6 }, (_, k) => {
      const point = round(310 + 42 * Math.sin((chunk * 6 + k) / 3) + 12 * rng(), 2);
      return {
        step: chunk * 6 + k,
        forecast: point,
        lower: round(point - 24 - 8 * rng(), 2),
        upper: round(point + 24 + 8 * rng(), 2),
      };
    }),
  rowCount: 48,
  fetchHint: hint("main.dbx_leaning.forecasting_results"),
  detail: {
    RUNNING: "training",
    SUCCEEDED: "48-step horizon written",
    FAILED: "target column 'trips' not present in loaded frame",
    CANCELLED: "cancelled at epoch 18; last checkpoint written",
  },
};

/* ---------------------------------------------------------------- *
 * mcmc
 * ---------------------------------------------------------------- */

/**
 * `primary_metric` is `max_rhat` and is null while rhat is non-finite —
 * genuinely null, for the first few messages of every run.
 *
 * There are NO per-chain (mu, log_sigma) POSITIONS in this payload, so no
 * fixture can conjure them: the live trace chart is blocked on a model-side
 * change, and inventing the field here would hide that. Chain health is what
 * exists, so chain health is what this emits.
 */
const MCMC_SCRIPT: ModelScript = {
  naturalCount: 15,
  metricLabel: "max_rhat",
  durationS: 240,
  progress: (count, rng) => {
    const chains = 8;
    const drawsTotal = count * 200;
    const burnIn = Math.min(1000, Math.round(drawsTotal / 3));
    return (i) => {
      const p = (i + 1) / count;
      const drawsDone = (i + 1) * 200;
      // Chain 3 accepts nothing for the first 40% and then unsticks. The most
      // diagnostic number on the page, and it has to be visible moving.
      const perChain = Array.from({ length: chains }, (_, c) => {
        const stuck = c === 3 && p < 0.4;
        return stuck ? 0 : round(0.24 + 0.09 * rng() + 0.04 * p, 4);
      });
      const stuckChains = perChain.filter((a) => a < 0.01).length;
      const finite = perChain.filter((a) => a > 0);
      const mean = finite.reduce((s, a) => s + a, 0) / Math.max(1, perChain.length);
      return {
        // Transiently null at the start — real for this model.
        percent_complete: i < 2 ? null : round(100 * (drawsDone / drawsTotal), 2),
        // rhat is non-finite until the chains have moved at all.
        primary_metric: i < 3 ? null : round(1.006 + 1.4 * Math.exp(-4.2 * p), 4),
        payload: {
          draws_done: drawsDone,
          draws_total: drawsTotal,
          chains,
          parameters: ["mu", "log_sigma"],
          post_burn_in_draws: Math.max(0, drawsDone - burnIn),
          mean_acceptance: round(mean, 4),
          min_acceptance: round(Math.min(...perChain), 4),
          stuck_chains: stuckChains,
          per_chain_acceptance: perChain,
        },
      };
    };
  },
  logs: [
    { level: "INFO", phase: "input", text: "2000 trips sampled; x=trip_distance y=fare_amount" },
    { level: "INFO", phase: "run", text: "8 chains, 3000 draws, 1000 burn-in" },
    { level: "DEBUG", phase: "run", text: (i) => `draw ${(i + 1) * 200}: mean acceptance ${round(0.26 + (i % 5) * 0.006, 3)}` },
    { level: "WARNING", phase: "run", text: "chain 3 has accepted no proposals in 400 draws" },
  ],
  logSource: "model",
  preview: (rng, chunk) =>
    ["mu", "log_sigma"].map((parameter, k) => {
      const mean = k === 0 ? round(2.84 + 0.2 * rng(), 4) : round(-0.62 + 0.1 * rng(), 4);
      return {
        parameter,
        chunk,
        mean,
        sd: round(0.04 + 0.02 * rng(), 4),
        hdi_low: round(mean - 0.09, 4),
        hdi_high: round(mean + 0.09, 4),
        r_hat: round(1.002 + 0.006 * rng(), 4),
        ess: Math.round(1200 + 900 * rng()),
      };
    }),
  rowCount: 2,
  fetchHint: hint("main.dbx_leaning.mcmc_results"),
  detail: {
    RUNNING: "sampling",
    SUCCEEDED: "converged: max rhat 1.01",
    FAILED: "likelihood returned nan on chain 5",
    CANCELLED: "cancelled during sampling; partial chains discarded",
  },
};

/* ---------------------------------------------------------------- *
 * scenario
 * ---------------------------------------------------------------- */

/** The one model whose `percent_complete` is meaningful the whole way
 *  through. `primary_metric` is `best_objective` — a cost, so the running
 *  best is a running MINIMUM and is monotone non-increasing. */
const SCENARIO_SCRIPT: ModelScript = {
  naturalCount: 8,
  metricLabel: "best_objective",
  durationS: 90,
  progress: (count, rng) => {
    const perMessage = 9;
    const total = count * perMessage;
    let best = Number.POSITIVE_INFINITY;
    const demand = DEFAULT_GRID.demand;
    const capacity = DEFAULT_GRID.capacity;
    const unitCost = DEFAULT_GRID.unit_cost;
    return (i) => {
      const done = (i + 1) * perMessage;
      const idx = done - 1;
      // Decompose the flat scenario index back over the 6 x 4 x 3 grid, the
      // way the model iterates it.
      const d = demand[idx % demand.length] ?? 1;
      const c = capacity[Math.floor(idx / demand.length) % capacity.length] ?? 1;
      const u = unitCost[Math.floor(idx / (demand.length * capacity.length)) % unitCost.length] ?? 1;
      const served = round(1420 * Math.min(d, c) * (0.94 + 0.05 * rng()), 1);
      const shortfall = round(Math.max(0, 1420 * d - served), 1);
      const objective = round(served * u * 4.2 + shortfall * 11.5, 2);
      best = Math.min(best, objective);
      return {
        percent_complete: round(100 * (done / total), 2),
        primary_metric: round(best, 2),
        payload: {
          scenarios_done: done,
          scenarios_total: total,
          last_scenario: { demand: d, capacity: c, unit_cost: u },
          last_outcome: {
            objective,
            served,
            shortfall,
            idle: round(Math.max(0, 1420 * c - served), 1),
          },
        },
      };
    };
  },
  logs: [
    { level: "INFO", phase: "input", text: "30 days of history loaded, seed 7" },
    { level: "INFO", phase: "run", text: "grid 6 x 4 x 3 = 72 scenarios" },
    { level: "DEBUG", phase: "run", text: (i) => `scenario ${(i + 1) * 9} evaluated in ${round(0.08 + (i % 4) * 0.01, 3)}s` },
    { level: "INFO", phase: "run", text: (i) => `new best objective at scenario ${(i + 1) * 9}` },
  ],
  logSource: "model",
  preview: (rng, chunk) =>
    Array.from({ length: 6 }, (_, k) => {
      const idx = chunk * 6 + k;
      return {
        demand: DEFAULT_GRID.demand[idx % 6] ?? 1,
        capacity: DEFAULT_GRID.capacity[Math.floor(idx / 6) % 4] ?? 1,
        unit_cost: DEFAULT_GRID.unit_cost[Math.floor(idx / 24) % 3] ?? 1,
        objective: round(5800 + 2400 * rng(), 2),
        shortfall: round(220 * rng(), 1),
      };
    }),
  rowCount: 72,
  fetchHint: hint("main.dbx_leaning.scenario_results"),
  detail: {
    RUNNING: "sweeping 72 scenarios",
    SUCCEEDED: "72 of 72 scenarios evaluated",
    FAILED: "grid.demand contained a non-numeric entry",
    CANCELLED: "cancelled after 50 scenarios; partial grid written",
  },
};

/* ---------------------------------------------------------------- *
 * streaming_results
 * ---------------------------------------------------------------- */

/**
 * The provenance keys are the honest weak point of this fixture. The model
 * spreads `**self._provenance` into every payload, so extra keys certainly
 * exist — but `models.ts` does not name them, so `source` / `table` /
 * `data_synthetic` below are PLAUSIBLE, NOT DERIVED. Do not build a view that
 * reads them by name; the index signature is the only part that is contract.
 */
const STREAMING_SCRIPT: ModelScript = {
  naturalCount: 24,
  metricLabel: "window_mae",
  durationS: 66,
  progress: (count, rng) => (i) => {
    const done = i + 1;
    return {
      // Transiently null on the first message, before the first window closes.
      percent_complete: i === 0 ? null : round(100 * (done / count), 2),
      primary_metric: round(3.1 + 0.9 * rng() - 0.4 * (i / count), 4),
      payload: {
        windows_done: done,
        windows_total: count,
        origin: 120 + i * 40,
        source: "samples",
        table: "samples.nyctaxi.trips",
        data_synthetic: false,
      },
    };
  },
  logs: [
    { level: "INFO", phase: "input", text: "60 days loaded; window 120, step 40, horizon 12" },
    { level: "DEBUG", phase: "run", text: (i) => `window origin ${120 + i * 40} fitted` },
    { level: "INFO", phase: "run", text: (i) => `chunk flushed: ${12 * (i + 1)} rows cumulative` },
  ],
  logSource: "model",
  preview: (rng, chunk) =>
    Array.from({ length: 4 }, (_, k) => {
      const actual = round(288 + 60 * Math.sin((chunk * 4 + k) / 2.2) + 9 * rng(), 2);
      const predicted = round(actual + 11 * (rng() - 0.5), 2);
      return {
        origin: 120 + chunk * 160 + k * 40,
        horizon: k + 1,
        actual,
        predicted,
        abs_error: round(Math.abs(actual - predicted), 2),
      };
    }),
  rowCount: 288,
  fetchHint: hint("main.dbx_leaning.streaming_results_results"),
  detail: {
    RUNNING: "rolling origin evaluation",
    SUCCEEDED: "24 windows, 288 rows across 5 chunks",
    FAILED: "window 9 produced a nan prediction",
    CANCELLED: "cancelled mid-window; chunks already flushed are durable",
  },
};

/* ---------------------------------------------------------------- *
 * annealing
 * ---------------------------------------------------------------- */

/**
 * The one deliberately non-monotonic payload. `current_objective` goes DOWN
 * on purpose while `primary_metric` (`best_fare`) only ever goes up, and
 * `feasible` is false on purpose while the search crosses out of the capacity
 * constraint. A view showing only the current objective makes a working
 * search look like it is failing, which is the exact reading the model's
 * two-number split exists to prevent — so the fixture makes that failure mode
 * reproducible rather than theoretical.
 */
const ANNEALING_SCRIPT: ModelScript = {
  naturalCount: 30,
  metricLabel: "best_fare",
  durationS: 45,
  progress: (count, rng) => {
    const perMessage = 1000;
    const total = count * perMessage;
    const capacity = 1740;
    const startTemp = 18.4;
    const endTemp = 0.184;
    let best = 0;
    let accepted = 0;
    return (i) => {
      const p = (i + 1) / count;
      const iteration = (i + 1) * perMessage;
      const temperature = startTemp * (endTemp / startTemp) ** p;
      // A real walk: rising trend, big early excursions that shrink as the
      // temperature falls.
      const value = 940 + 520 * p + 180 * (rng() - 0.5) * (1 - p * 0.8);
      const weight = capacity * (0.9 + 0.22 * (rng() - 0.35) * (1 - p * 0.6));
      const feasible = weight <= capacity;
      const objective = value - 0.8 * Math.max(0, weight - capacity);
      if (feasible) best = Math.max(best, value);
      const acceptance = 0.62 * Math.exp(-2.9 * p) + 0.03;
      accepted += Math.round(perMessage * acceptance);
      return {
        percent_complete: round(100 * (iteration / total), 2),
        primary_metric: round(best, 2),
        payload: {
          iteration,
          iterations_total: total,
          temperature: round(temperature, 4),
          current_objective: round(objective, 2),
          current_value: round(value, 2),
          current_weight: round(weight, 1),
          capacity,
          feasible,
          acceptance_rate: round(acceptance, 4),
          accepted_total: accepted,
          // Of the BEST selection, not the current one.
          items_selected: 54 + Math.round(10 * p + 3 * rng()),
        },
      };
    };
  },
  logs: [
    { level: "INFO", phase: "input", text: "240 trips offered; capacity 1740 of 6960 minutes" },
    { level: "INFO", phase: "build", text: "start temperature 18.4 derived from mean fare" },
    { level: "DEBUG", phase: "run", text: (i) => `iteration ${(i + 1) * 1000}: temperature ${round(18.4 * 0.85 ** i, 3)}` },
    { level: "INFO", phase: "run", text: (i) => `new best fare ${round(940 + i * 17.3, 2)}` },
    // progress_every doubles as the cancellation-check interval, so a cancel
    // can take up to 1000 iterations to land. Do not render that as hung.
    { level: "DEBUG", phase: "run", text: "cancellation check (every 1000 iterations)" },
  ],
  logSource: "model",
  preview: (rng, chunk) =>
    Array.from({ length: 6 }, (_, k) => ({
      trip_id: `R${String(chunk * 6 + k).padStart(3, "0")}`,
      minutes: Math.round(14 + 30 * rng()),
      fare: round(9 + 26 * rng(), 2),
      selected: rng() > 0.35,
    })),
  rowCount: 240,
  fetchHint: hint("main.dbx_leaning.annealing_results"),
  detail: {
    RUNNING: "annealing",
    SUCCEEDED: "best fare 1462.80, 64 trips, 1731 of 1740 minutes used",
    FAILED: "capacity_fraction 0 leaves no feasible selection",
    CANCELLED: "cancelled at iteration 21000; best selection retained",
  },
};

/* ---------------------------------------------------------------- *
 * bayesian_ab
 * ---------------------------------------------------------------- */

/**
 * Five stages, closed form, whole run in milliseconds. Three things here are
 * contract and easy to get wrong:
 *
 *  - `progress_shape: "stages"` is a rendering instruction: do not join these
 *    points with a line.
 *  - `prob_b_beats_a` / `expected_loss` / `lift` / `decision` / `conclusive`
 *    are ADDED as their stages complete. Absent, not null — `in` or
 *    `!== undefined`, never a null check.
 *  - `decision` is an arm LABEL or the literal "inconclusive". Never "A"/"B",
 *    never a boolean. An arm can lead without being conclusive.
 *
 * `stage_index` is ONE-BASED. `models/bayesian_ab/model.py` iterates
 * `enumerate(STAGES, start=1)` and emits after the stage body has run, so the
 * field is a COUNT OF COMPLETED STAGES, not an array index — the first
 * message carries 1, the last carries 5, and `percent_complete` is
 * `100 * index / 5` giving 20..100 with no zero. A view indexing
 * `BAYESIAN_AB_STAGES[stage_index]` is off by one and must subtract.
 *
 * This fixture emitted it 0-based until the ambiguity was resolved against
 * the source, which made a completed run render as four of five stages done.
 */
const BAYESIAN_AB_SCRIPT: ModelScript = {
  naturalCount: BAYESIAN_AB_STAGES.length,
  maxCount: BAYESIAN_AB_STAGES.length,
  metricLabel: "prob_b_beats_a",
  durationS: 0.4,
  progress: (count, rng, nullish) => {
    const trialsA = 4128;
    const trialsB = 1832;
    const successesA = 1719;
    const successesB = 812;
    const prior = { alpha: 1, beta: 1 };
    const probBBeatsA = round(0.963 + 0.01 * rng(), 4);
    return (i) => {
      // `i` is the array index; the wire field is the 1-based completed count.
      const stageIndex = i + 1;
      const stage = BAYESIAN_AB_STAGES[i] ?? "decision";
      // The three posterior_* fields are "null until the posteriors stage has
      // run for this arm". Since `posteriors` IS stage 0 and a message is
      // emitted as each stage completes, they are populated from the first
      // message of a normal run — which leaves the null branch untested. The
      // null-heavy fixture is where it gets exercised.
      const posteriorsDone = !nullish;
      const arms = [
        {
          role: "A" as const,
          label: "weekday_hours",
          trials: trialsA,
          successes: successesA,
          posterior_alpha: posteriorsDone ? prior.alpha + successesA : null,
          posterior_beta: posteriorsDone ? prior.beta + trialsA - successesA : null,
          posterior_mean: posteriorsDone
            ? round((prior.alpha + successesA) / (prior.alpha + prior.beta + trialsA), 5)
            : null,
        },
        {
          role: "B" as const,
          label: "weekend_hours",
          trials: trialsB,
          successes: successesB,
          posterior_alpha: posteriorsDone ? prior.alpha + successesB : null,
          posterior_beta: posteriorsDone ? prior.beta + trialsB - successesB : null,
          posterior_mean: posteriorsDone
            ? round((prior.alpha + successesB) / (prior.alpha + prior.beta + trialsB), 5)
            : null,
        },
      ];
      const payload: Record<string, unknown> = {
        stage,
        stage_index: stageIndex,
        stages_total: BAYESIAN_AB_STAGES.length,
        progress_shape: "stages",
        comparison: "weekend_fare",
        outcome: "success = trip fare at or above the pooled median fare of 14.50",
        prior,
        credible_mass: 0.95,
        arms,
      };
      if (stageIndex > 1) payload.prob_b_beats_a = probBBeatsA;
      if (stageIndex > 2) payload.expected_loss = { A: round(0.0181, 5), B: round(0.0004, 5) };
      if (stageIndex > 3) payload.lift = { absolute: 0.0272, relative: 0.0654, hdi_low: 0.0031, hdi_high: 0.0513 };
      if (stageIndex > 4) {
        payload.decision = "weekend_hours";
        payload.conclusive = true;
      }
      return {
        percent_complete: round(100 * ((i + 1) / count), 2),
        // Null until the comparison stage computes it.
        primary_metric: i >= 1 ? probBBeatsA : null,
        payload,
      };
    };
  },
  logs: [
    { level: "INFO", phase: "input", text: "60 days of trips; pooled median fare 14.50" },
    { level: "INFO", phase: "run", text: (i) => `stage ${BAYESIAN_AB_STAGES[i % BAYESIAN_AB_STAGES.length] ?? "decision"} complete` },
    { level: "DEBUG", phase: "run", text: "closed form; no sampling required" },
  ],
  logSource: "model",
  preview: (_rng, chunk) =>
    [
      { arm: "A", label: "weekday_hours", trials: 4128, successes: 1719, posterior_mean: 0.41643, hdi_low: 0.40142, hdi_high: 0.43152, chunk },
      { arm: "B", label: "weekend_hours", trials: 1832, successes: 812, posterior_mean: 0.44329, hdi_low: 0.42054, hdi_high: 0.46618, chunk },
    ],
  rowCount: 2,
  fetchHint: hint("main.dbx_leaning.bayesian_ab_results"),
  detail: {
    RUNNING: "computing posteriors",
    SUCCEEDED: "weekend_hours leads, conclusive at threshold 0.95",
    // The one model that validates its own config, so a typo is a FAILED run.
    FAILED: "unknown comparison 'weekend_fares'; expected one of weekend_fare, long_trip_speed",
    CANCELLED: "cancelled before the decision stage",
  },
};

/* ---------------------------------------------------------------- *
 * neural_net
 * ---------------------------------------------------------------- */

/**
 * Two progress LEVELS interleaved on one stream — "batch" and "epoch" — so a
 * chart keyed on `epoch` gets several points per x. Key on `seq`, or filter
 * to `level === "epoch"`. The fixture emits them in the real 2-batch-then-1-
 * epoch rhythm so that bug reproduces here.
 *
 * `best_val_accuracy` is null through the FIRST epoch's batch samples: the
 * best checkpoint only updates at epoch end. Null means "no epoch has
 * finished", not zero.
 */
const NEURAL_NET_SCRIPT: ModelScript = {
  naturalCount: 36,
  metricLabel: "val_accuracy",
  durationS: 12,
  progress: (count, rng, nullish) => {
    const batchesPerEpoch = 24;
    const epochsTotal = Math.max(1, Math.ceil(count / 3));
    const totalSteps = epochsTotal * batchesPerEpoch;
    let bestAcc: number | null = null;
    return (i) => {
      const epoch = Math.floor(i / 3);
      const slot = i % 3;
      const level = slot < 2 ? "batch" : "epoch";
      const batch = slot < 2 ? (slot + 1) * 8 : batchesPerEpoch;
      const steps = epoch * batchesPerEpoch + batch;
      const p = steps / totalSteps;
      const trainLoss = 1.02 * Math.exp(-1.9 * p) + 0.21;
      const valLoss = trainLoss * 1.09 + 0.03 * (rng() - 0.4);
      const valAccuracy = round(0.42 + 0.36 * (1 - Math.exp(-2.7 * p)), 4);
      // Null through the FIRST epoch's batch samples: best-checkpoint
      // tracking only updates at epoch end. Null means "no epoch has
      // finished", not "zero".
      if (level === "epoch" && !nullish) {
        bestAcc = bestAcc === null ? valAccuracy : Math.max(bestAcc, valAccuracy);
      }
      return {
        // Monotonic across BOTH levels because it counts batch steps.
        percent_complete: round(100 * p, 2),
        primary_metric: valAccuracy,
        payload: {
          level,
          epoch,
          epochs_total: epochsTotal,
          batch,
          batches_per_epoch: batchesPerEpoch,
          train_loss: round(trainLoss, 5),
          val_loss: round(valLoss, 5),
          macro_f1: round(0.31 + 0.38 * (1 - Math.exp(-2.4 * p)), 4),
          grad_norm: round(2.4 * Math.exp(-1.6 * p) + 0.08 + 0.2 * rng(), 4),
          learning_rate: round(0.01 * 0.9 ** epoch, 6),
          best_val_accuracy: bestAcc,
          // ~55/30/15 classes on purpose: accuracy without this beside it
          // flatters the model.
          baseline_accuracy: 0.552,
          device: "cpu",
          data_synthetic: nullish ? null : false,
        },
      };
    };
  },
  logs: [
    { level: "INFO", phase: "input", text: "4000 rows; class cuts at the 0.55 / 0.85 training quantiles" },
    { level: "INFO", phase: "build", text: "mlp 6 -> 32 -> 16 -> 3 on cpu" },
    { level: "DEBUG", phase: "run", text: (i) => `epoch ${Math.floor(i / 3)} batch ${8 * (1 + (i % 3))}` },
    { level: "INFO", phase: "run", text: (i) => `epoch ${Math.floor(i / 3)} val_accuracy improved` },
  ],
  logSource: "model",
  preview: (rng, chunk) =>
    NEURAL_NET_CLASS_LABELS.map((label, k) => ({
      class_index: k,
      class_label: label,
      precision: round(0.58 + 0.3 * rng(), 4),
      recall: round(0.52 + 0.34 * rng(), 4),
      f1: round(0.55 + 0.3 * rng(), 4),
      support: [551, 297, 152][k] ?? 100,
      chunk,
    })),
  rowCount: 3,
  fetchHint: hint("main.dbx_leaning.neural_net_results"),
  detail: {
    RUNNING: "training on cpu",
    SUCCEEDED: "val_accuracy 0.771 against a 0.552 majority-class baseline",
    FAILED: "torch not available in the job environment",
    CANCELLED: "cancelled at epoch 5; best checkpoint written",
  },
};

/* ---------------------------------------------------------------- *
 * Fallback — a view whose model is not one of the nine
 * ---------------------------------------------------------------- */

/** The gallery is handed whatever views exist, including a new one written
 *  before `models.ts` knows about it. It gets common-envelope-fields-only
 *  traffic and an empty payload, which is also the exact input the generic
 *  fallback view is specified against. */
const GENERIC_SCRIPT: ModelScript = {
  naturalCount: 24,
  metricLabel: "primary_metric",
  durationS: 60,
  progress: (count, rng) => (i) => ({
    percent_complete: round(100 * ((i + 1) / count), 2),
    primary_metric: round(50 + 40 * rng(), 3),
    payload: {},
  }),
  logs: [
    { level: "INFO", phase: "run", text: (i) => `step ${i}` },
    { level: "DEBUG", phase: "run", text: "no model-specific payload declared" },
  ],
  logSource: "model",
  preview: (_rng, chunk) => [{ chunk, note: "no preview shape declared for this model" }],
  rowCount: 1,
  fetchHint: hint("main.dbx_leaning.unknown_results"),
  detail: { RUNNING: "running", SUCCEEDED: "done" },
};

/* ---------------------------------------------------------------- *
 * ortools_jobshop
 * ---------------------------------------------------------------- */

/**
 * The inverse of the Gurobi scripts, and deliberately so.
 *
 * A MIP callback fires constantly and its driver's job is to SUPPRESS.
 * CP-SAT's solution callback fires only on an improving solution — a handful
 * of times across a whole run — so this script emits few, widely spaced
 * messages. A fixture that produced Gurobi-like chatter here would teach a
 * view to expect traffic that never comes, and hide the case that matters:
 * long stretches where nothing improves and the signature is correctly still.
 *
 * `percent_complete` is real here, unlike the Gurobi models' permanent null,
 * but it is elapsed time against the time limit — a TIME fraction, not a
 * search fraction. The final sample reports 100 because the search finished,
 * which is the refinement that stops an optimal-in-3-seconds run looking dead
 * at 5%.
 *
 * `conflicts` and `branches` are ABSENT on early messages, not null: CP-SAT
 * does not hand them over until it has some.
 */
const ORTOOLS_JOBSHOP_SCRIPT: ModelScript = {
  naturalCount: 6,
  maxCount: 24,
  metricLabel: "relative_gap",
  durationS: 60,
  progress: (count, rng, nullish) => {
    const timeLimit = 60;
    const nJobs = 60;
    const nMachines = 4;
    const nOperations = 214;
    // A trivial machine-load bound, reached almost immediately and then
    // barely moving — which is what makes the gap curve almost entirely
    // incumbent movement on this model.
    const bound = 1836;
    let incumbent = 2410;
    let found = 0;
    return (i) => {
      const last = i === count - 1;
      found += 1;
      // Improvements shrink as the search goes on; the last few barely move.
      incumbent = Math.max(bound + 12, incumbent - (140 - i * 18) * (0.7 + rng() * 0.6));
      const inc = nullish && i === 0 ? null : round(incumbent, 0);
      const bnd = i === 0 ? null : bound; // pre-search bound is a real infinity
      const gap = inc !== null && bnd !== null ? round((inc - bnd) / inc, 5) : null;
      const elapsed = round((timeLimit * (i + 1)) / count, 2);
      const payload: Record<string, unknown> = {
        incumbent: inc,
        best_bound: bnd,
        gap,
        solutions_found: found,
        wall_time: elapsed,
        n_jobs: nJobs,
        n_machines: nMachines,
        n_operations: nOperations,
        percent_complete_basis: "elapsed_solver_time_against_time_limit",
        final: last,
      };
      // Absent until the solver has some — `in`, never a null check.
      if (i >= 1) payload.conflicts = 120 + i * 340;
      if (i >= 1) payload.branches = 900 + i * 2100;
      if (last) payload.solver_status = "OPTIMAL";
      return {
        elapsed_seconds: elapsed,
        // A time fraction. 100 on the final sample because the search
        // terminated on its own.
        percent_complete: last ? 100 : round((100 * elapsed) / timeLimit, 2),
        primary_metric: gap,
        payload,
      };
    };
  },
  logs: [
    { level: "INFO", phase: "input", text: "24 jobs standing for 1,203 transactions" },
    { level: "INFO", phase: "build", text: "214 operations across 4 machines" },
    { level: "INFO", phase: "solve", text: (i) => `improved incumbent #${i + 1}` },
    // The log callback is what polls for cancellation — CP-SAT has no
    // POLLING equivalent, so without it a cancel on a stalled search is not
    // seen until the time limit.
    { level: "DEBUG", phase: "solve", text: "cp-sat log (also the cancel poll)" },
  ],
  logSource: "model",
  preview: (rng, chunk) =>
    Array.from({ length: 8 }, (_, k) => {
      const start = Math.round(40 * (chunk * 8 + k) + 120 * rng());
      const duration = Math.round(12 + 48 * rng());
      return {
        job_id: chunk * 8 + k,
        job_label: `Tokyo Tidbits x${Math.round(12 + 40 * rng())}`,
        operation_index: k % 4,
        machine_id: k % 4,
        machine_label: ["mix", "bake", "decorate", "pack"][k % 4],
        start_minute: start,
        duration_minutes: duration,
        end_minute: start + duration,
      };
    }),
  rowCount: 214,
  fetchHint: hint("main.dbx_leaning.results_ortools_jobshop"),
  detail: {
    RUNNING: "feasible: makespan 2130 min, gap 13.8%",
    SUCCEEDED: "optimal: makespan 1858 min",
    FAILED: "MODEL_INVALID",
    CANCELLED: "cancelled: makespan 2210 min kept",
    INFEASIBLE: "deadline 900 min cannot fit 214 operations",
  },
};

/* ---------------------------------------------------------------- *
 * panel_fit
 * ---------------------------------------------------------------- */

/**
 * The only script here whose units can FAIL while the run succeeds, which is
 * the whole reason the model exists — so the fixture must produce failures on
 * the ordinary path, not only in an edge case. A run where everything fits
 * would make the view's headline untestable.
 *
 * `groups_fitted + groups_failed === groups_done` on every message, always.
 * There is a test.
 *
 * `data_synthetic` is true even on the non-nullish fixture, and that is
 * faithful rather than lazy: `DEFAULT_PANEL_TABLE` has never been landed, so
 * a default run really does take the generated panel every time.
 */
const PANEL_FIT_SCRIPT: ModelScript = {
  naturalCount: 24,
  metricLabel: "median_r_squared",
  durationS: 20,
  progress: (count, rng, nullish) => {
    const total = count;
    const reasons = [
      "too_few_observations",
      "zero_predictor_variance",
      "singular_design",
      "non_finite_result",
    ] as const;
    let fitted = 0;
    let failed = 0;
    const counts: Record<string, number> = {};
    const rsq: number[] = [];
    return (i) => {
      const done = i + 1;
      // Roughly one in six fails, which is a realistic panel: small countries
      // with three observations are the norm, not the exception.
      const fails = rng() < 0.17;
      const reason = fails ? reasons[Math.floor(rng() * reasons.length) % 4] : null;
      const observations = fails ? Math.round(1 + rng() * 3) : Math.round(18 + rng() * 42);
      let r: number | null = null;
      if (fails) {
        failed += 1;
        if (reason) counts[reason] = (counts[reason] ?? 0) + 1;
      } else {
        fitted += 1;
        r = round(0.55 + rng() * 0.42, 4);
        rsq.push(r);
      }
      const median =
        rsq.length > 0 ? round([...rsq].sort((a, b) => a - b)[Math.floor(rsq.length / 2)] ?? 0, 4) : null;
      return {
        elapsed_seconds: round((20 * done) / total, 2),
        // Groups done over groups total. No estimation — the denominator is
        // known before the first fit.
        percent_complete: round((100 * done) / total, 2),
        primary_metric: median,
        payload: {
          groups_done: done,
          groups_total: total,
          groups_fitted: fitted,
          groups_failed: failed,
          failure_counts: { ...counts },
          group_key: `G${String(done).padStart(3, "0")}`,
          group_label: `country-${done}`,
          group_status: fails ? "failed" : "fitted",
          group_failure_reason: reason,
          group_r_squared: r,
          n_observations: observations,
          // Rows the group HAD, against the ones that survived the null drop.
          rows_seen: observations + (fails ? Math.round(rng() * 2) : 0),
          metric_higher_is_better: true,
          degree: 1,
          chunks_emitted: Math.floor(done / 12),
          data_source: "synthetic",
          // True even here: the default table has never been created.
          data_synthetic: nullish ? null : true,
          data_rows: 4800,
          data_fallback_reason: "main.dbx_leaning.owid_country_year does not exist",
        },
      };
    };
  },
  logs: [
    { level: "INFO", phase: "input", text: "panel table not found; using the generated panel" },
    { level: "INFO", phase: "run", text: (i) => `group ${i + 1} fitted` },
    { level: "WARNING", phase: "run", text: "too_few_observations: 3 rows below the degree + 2 floor" },
  ],
  logSource: "model",
  preview: (rng, chunk) =>
    Array.from({ length: 6 }, (_, k) => {
      const fails = rng() < 0.17;
      return {
        group_key: `G${String(chunk * 6 + k).padStart(3, "0")}`,
        group_label: `country-${chunk * 6 + k}`,
        status: fails ? "failed" : "fitted",
        failure_reason: fails ? "too_few_observations" : null,
        n_observations: fails ? 3 : Math.round(20 + 40 * rng()),
        slope: fails ? null : round(0.12 + rng() * 0.3, 5),
        r_squared: fails ? null : round(0.6 + rng() * 0.35, 4),
      };
    }),
  rowCount: 180,
  fetchHint: hint("main.dbx_leaning.results_panel_fit"),
  detail: {
    RUNNING: "fitting group 14 of 24",
    SUCCEEDED: "20 fitted, 4 failed",
    FAILED: "response column not present in the panel",
    CANCELLED: "cancelled after 11 groups; those fits kept",
    // Not SUCCEEDED (row_count looks healthy) and not FAILED (nothing went
    // wrong) — see docs/message-envelope-spec.md.
    INFEASIBLE: "every group failed to fit",
  },
};

const SCRIPTS: Record<string, ModelScript> = {
  gurobi_scheduling: GUROBI_SCHEDULING_SCRIPT,
  gurobi_routing: GUROBI_ROUTING_SCRIPT,
  forecasting: FORECASTING_SCRIPT,
  mcmc: MCMC_SCRIPT,
  scenario: SCENARIO_SCRIPT,
  streaming_results: STREAMING_SCRIPT,
  annealing: ANNEALING_SCRIPT,
  bayesian_ab: BAYESIAN_AB_SCRIPT,
  neural_net: NEURAL_NET_SCRIPT,
  ortools_jobshop: ORTOOLS_JOBSHOP_SCRIPT,
  panel_fit: PANEL_FIT_SCRIPT,
};

/** Whether this model has a hand-written script or falls back to
 *  common-fields-only traffic. The gallery says so out loud. */
export function hasScript(model: string): boolean {
  return model in SCRIPTS;
}

function scriptFor(model: string): ModelScript {
  return SCRIPTS[model] ?? GENERIC_SCRIPT;
}

/* ================================================================== *
 * Timeline planning
 * ================================================================== */

/** Fixed epoch so `ts` is stable across reloads — a fixture that moved with
 *  the wall clock could not be screenshot-diffed. */
const RUN_START_MS = Date.UTC(2026, 7, 24, 9, 15, 0);

type Entry =
  | { at: number; kind: "progress"; i: number }
  | { at: number; kind: "log"; i: number }
  | { at: number; kind: "result"; chunk: number }
  | { at: number; kind: "skip"; count: number };

interface RunPlan {
  entries: readonly Entry[];
  progressCount: number;
  chunks: number;
}

/**
 * The full timeline of a complete run, before any lifecycle state is applied.
 *
 * Positions are fractions of the run, so a state is a prefix: RUNNING is the
 * first 60% of exactly the same entries SUCCEEDED shows all of, with exactly
 * the same seq numbers. That is what makes the harness read as one run
 * observed at different moments rather than eight unrelated runs.
 */
function planRun(model: string, fixture: FixtureName): RunPlan {
  const script = scriptFor(model);
  const plan = FIXTURE_PLANS[fixture];
  const requested = plan.progressCount ?? script.naturalCount;
  const progressCount = Math.min(requested, script.maxCount ?? requested);
  // Logs scale down with a clamped progress count so bayesian_ab's "dense"
  // fixture is not five progress messages drowned in 1200 log lines.
  const logCount =
    plan.progressCount !== null && progressCount < plan.progressCount
      ? Math.min(plan.logCount, progressCount * 3)
      : plan.logCount;
  const chunks = progressCount === 0 ? 0 : plan.resultChunks;

  const entries: Entry[] = [];
  for (let i = 0; i < progressCount; i += 1) {
    entries.push({ at: (i + 0.5) / progressCount, kind: "progress", i });
  }
  for (let i = 0; i < logCount; i += 1) {
    entries.push({ at: (i + 0.5) / logCount, kind: "log", i });
  }
  for (let c = 0; c < chunks; c += 1) {
    // A single result lands at the end; several are spread through the run,
    // which is what makes a partially-observed chunked run possible.
    entries.push({ at: chunks === 1 ? 0.995 : (c + 1) / (chunks + 1), kind: "result", chunk: c });
  }
  if (plan.gapSize > 0) entries.push({ at: 0.5, kind: "skip", count: plan.gapSize });

  // Stable sort: equal fractions keep insertion order, which puts progress
  // before logs at the same instant. Deterministic either way.
  entries.sort((a, b) => a.at - b.at);
  return { entries, progressCount, chunks };
}

/* ================================================================== *
 * Assembly
 * ================================================================== */

export function fixtureRunId(model: string, fixture: FixtureName): string {
  // Shaped like a real run id (`run-` + 12 hex) so anything that pattern-
  // matches on it in a view behaves the same way it will in production.
  const h = hashSeed(`${model}|${fixture}`).toString(16).padStart(8, "0");
  return `run-${h}${hashSeed(fixture).toString(16).slice(0, 4)}`;
}

interface Assembled {
  runId: string;
  messages: Message[];
  gaps: Gap[];
  connection: ConnectionState;
  /** Set when the run is terminal but no status message was ever seen — the
   *  `empty` fixture's whole point. */
  terminalWithoutMessage: RunStatus | null;
}

function assemble(model: string, fixture: FixtureName, state: UiRunState): Assembled {
  const script = scriptFor(model);
  const fixturePlan = FIXTURE_PLANS[fixture];
  const recipe = STATE_RECIPES[state];
  const runId = fixtureRunId(model, fixture);
  // Seeded from model|fixture only, NOT the state — so the numbers at entry
  // index k are the same in every state and the prefix property holds.
  const rng = makeRng(hashSeed(`${model}|${fixture}`));
  // Previews draw from their own stream so that dropping a result chunk —
  // which FAILED and INFEASIBLE do — cannot shift the numbers in the progress
  // messages that follow it. Without this, RUNNING would stop being a literal
  // prefix of SUCCEEDED and the harness would read as eight unrelated runs.
  const previewRng = makeRng(hashSeed(`${model}|${fixture}|preview`));
  const plan = planRun(model, fixture);
  const pointAt = script.progress(
    Math.max(1, plan.progressCount),
    rng,
    fixturePlan.forceNullPercent && fixturePlan.forceNullMetric,
  );

  const messages: Message[] = [];
  const gaps: Gap[] = [];
  let seq = 0;

  const common = (at: number) => ({
    run_id: runId,
    seq: seq++,
    ts: RUN_START_MS + Math.round(at * script.durationS * 1000),
  });

  for (const status of recipe.live) {
    const at = status === "QUEUED" ? 0 : 0.01;
    const msg: StatusMessage = {
      ...common(at),
      type: "status",
      status,
      detail: script.detail[status] ?? null,
    };
    messages.push(msg);
  }

  const cut = Math.round(plan.entries.length * recipe.fraction);
  const visible = plan.entries.slice(0, cut);
  const resultEntries = visible.filter((e) => e.kind === "result");
  const keepPlannedResults = recipe.results === "planned";
  const lastPlannedChunk = resultEntries.at(-1)?.chunk ?? -1;

  for (const entry of visible) {
    switch (entry.kind) {
      case "skip": {
        // Seq numbers spent on client_visible:false logs the live path
        // dropped and the backfill endpoint filters out too. The messages are
        // never coming; this gap does not close.
        gaps.push({ from: seq, to: seq + entry.count - 1 });
        seq += entry.count;
        break;
      }
      case "progress": {
        const point = pointAt(entry.i);
        const msg: ProgressMessage = {
          ...common(entry.at),
          type: "progress",
          elapsed_seconds: round(entry.at * script.durationS, 2),
          percent_complete: fixturePlan.forceNullPercent ? null : point.percent_complete,
          primary_metric: fixturePlan.forceNullMetric ? null : point.primary_metric,
          primary_metric_label: fixturePlan.forceNullMetric ? null : script.metricLabel,
          payload: point.payload,
        };
        messages.push(msg);
        break;
      }
      case "log": {
        const template = script.logs[entry.i % script.logs.length];
        if (template === undefined) break;
        const msg: LogMessage = {
          ...common(entry.at),
          type: "log",
          message: typeof template.text === "string" ? template.text : template.text(entry.i),
          level: template.level,
          source: entry.i === 0 ? "job" : script.logSource,
          phase: template.phase,
          // Backfill is the only way a false one reaches a client, and the
          // gappy fixture is the backfilled one.
          client_visible: !(fixture === "gappy" && entry.i % 7 === 6),
        };
        messages.push(msg);
        break;
      }
      case "result": {
        if (!keepPlannedResults) break;
        const msg: ResultMessage = {
          ...common(entry.at),
          type: "result",
          preview: script.preview(previewRng, entry.chunk),
          row_count: Math.max(1, Math.round(script.rowCount / Math.max(1, plan.chunks))),
          fetch_hint: script.fetchHint(runId),
          chunk_index: entry.chunk,
          // Complete only once a final:true has been seen. A still-running
          // run has not written its last chunk, however many it has written.
          final: recipe.terminal !== null && entry.chunk === lastPlannedChunk && entry.chunk === plan.chunks - 1,
        };
        messages.push(msg);
        break;
      }
    }
  }

  if (recipe.closing !== undefined && plan.entries.length > 0) {
    const msg: LogMessage = {
      ...common(recipe.fraction),
      type: "log",
      message: recipe.closing.text,
      level: recipe.closing.level,
      source: "job",
      phase: "run",
      client_visible: true,
    };
    messages.push(msg);
  }

  if (recipe.results === "incumbent" || recipe.results === "empty") {
    const empty = recipe.results === "empty";
    const msg: ResultMessage = {
      ...common(recipe.fraction),
      type: "result",
      preview: empty ? [] : script.preview(previewRng, 0),
      // 0 is load-bearing: "reached the write, wrote nothing" is a different
      // fact from "never got there", and only row_count tells them apart.
      row_count: empty ? 0 : Math.max(1, Math.round(script.rowCount * recipe.fraction)),
      fetch_hint: script.fetchHint(runId),
      chunk_index: 0,
      final: true,
    };
    messages.push(msg);
  }

  if (recipe.terminal !== null) {
    const msg: StatusMessage = {
      ...common(1),
      type: "status",
      status: recipe.terminal,
      detail: script.detail[recipe.terminal] ?? null,
    };
    messages.push(msg);
  }

  // `empty` means empty: not even the status messages. The client learned the
  // outcome from GET /api/runs/{id} and saw nothing on the stream.
  if (fixture === "empty") {
    return {
      runId,
      messages: [],
      gaps: [],
      connection: recipe.connection,
      terminalWithoutMessage: recipe.terminal,
    };
  }

  return { runId, messages, gaps, connection: recipe.connection, terminalWithoutMessage: null };
}

/* ================================================================== *
 * Public API
 * ================================================================== */

const messageCache = new Map<string, readonly Message[]>();
const snapshotCache = new Map<string, RunSnapshot>();

const cacheKey = (model: string, fixture: FixtureName, state: UiRunState | null) =>
  `${model}|${fixture}|${state ?? "none"}`;

/**
 * The raw envelope stream for one (model, fixture, state).
 *
 * Exposed so other suites can feed a real `RunStore`, the hub, or IndexedDB
 * rather than only the finished snapshot. Cached — and the cache is only safe
 * because generation is deterministic; if that ever stops being true, this
 * memo becomes a bug.
 */
export function makeMessages(
  model: string,
  fixture: FixtureName,
  state: UiRunState | null,
): readonly Message[] {
  if (state === null) return [];
  const key = cacheKey(model, fixture, state);
  const cached = messageCache.get(key);
  if (cached !== undefined) return cached;
  const built = assemble(model, fixture, state).messages;
  messageCache.set(key, built);
  return built;
}

/**
 * A `RunSnapshot` as the app would hold it.
 *
 * `state === null` is "no run selected": an untouched store, `hydrated:false`
 * — which is NOT the same as the `empty` fixture, where hydration has
 * happened and genuinely returned nothing. A view has to tell those apart.
 */
export function makeSnapshot(
  model: string,
  fixture: FixtureName,
  state: UiRunState | null,
): RunSnapshot {
  const key = cacheKey(model, fixture, state);
  const cached = snapshotCache.get(key);
  if (cached !== undefined) return cached;

  if (state === null) {
    const snap = new RunStore(fixtureRunId(model, fixture)).getSnapshot();
    snapshotCache.set(key, snap);
    return snap;
  }

  const built = assemble(model, fixture, state);
  // Through the real store, so every derived field is derived by the code
  // that will derive it in production.
  const store = new RunStore(built.runId);
  store.ingest(built.messages, { hydrate: true });
  for (const gap of built.gaps) store.addGap(gap);
  if (built.terminalWithoutMessage !== null) store.markTerminal(built.terminalWithoutMessage);
  store.setConnection(built.connection, 0);

  const snap = store.getSnapshot();
  snapshotCache.set(key, snap);
  return snap;
}

/** Drop the memo. Only useful in tests that want to prove determinism from a
 *  cold start rather than reading the same object back. */
export function resetFixtureCache(): void {
  messageCache.clear();
  snapshotCache.clear();
}
