/**
 * Everything the `scenario` view derives from the stream, as pure functions.
 *
 * Kept out of the components because this is the part that can be *wrong*:
 * where the scan head sits, which cell holds the best objective so far, and
 * how much of that is a real placement rather than an interpolation. A
 * component that computes those inline can only be checked by rendering it.
 *
 * The sweep order is not a guess. `ScenarioModel.scenarios()` iterates
 * `itertools.product` over `sorted(self.grid)` — so the key order is
 * capacity, demand, unit_cost, capacity varying slowest and unit_cost
 * fastest. Laid out as a demand (columns) x capacity (rows) grid that is
 * exactly a left-to-right, top-to-bottom raster, with each cell dwelt on once
 * per unit-cost multiplier. Enumeration in a known order is the whole
 * difference between this model and Gurobi's heuristic search, and it is why
 * the animation is a scan.
 */

import type { ProgressMessage, ResultMessage } from "@/lib/envelope";
import { payloadOf } from "@/components/models/contract";
import { DEFAULT_GRID, type ScenarioProgressPayload } from "@/lib/models";

/** Columns. The view is fixed at the default grid's axes — a run's own grid
 *  is in its config, which a `ModelView` is never handed. See `locateCell`
 *  for what happens to a run that used a different one. */
export const SCAN_DEMAND: readonly number[] = DEFAULT_GRID.demand;
/** Rows. */
export const SCAN_CAPACITY: readonly number[] = DEFAULT_GRID.capacity;
export const SCAN_COLS = SCAN_DEMAND.length;
export const SCAN_ROWS = SCAN_CAPACITY.length;
export const SCAN_CELLS = SCAN_COLS * SCAN_ROWS;

/** Multipliers arrive as JSON numbers straight from the config list, so an
 *  exact match is the normal case; the epsilon is only there so a value that
 *  survived a float round trip does not silently fall through to the
 *  proportional fallback. */
const AXIS_EPSILON = 1e-9;

function finite(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function clamp(value: number, low: number, high: number): number {
  return Math.min(high, Math.max(low, value));
}

/**
 * `last_scenario` and `last_outcome` are typed `unknown` in
 * `ScenarioProgressPayload` for the honest reason that nothing validates
 * them server-side. Both are flat dicts of floats when they arrive at all.
 */
export function asNumberRecord(value: unknown): Record<string, number> | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const out: Record<string, number> = {};
  let any = false;
  for (const [key, raw] of Object.entries(value as Record<string, unknown>)) {
    const num = finite(raw);
    if (num !== null) {
      out[key] = num;
      any = true;
    }
  }
  return any ? out : null;
}

function axisIndex(axis: readonly number[], value: number): number | null {
  for (let i = 0; i < axis.length; i += 1) {
    const candidate = axis[i];
    if (candidate !== undefined && Math.abs(candidate - value) < AXIS_EPSILON) return i;
  }
  return null;
}

export interface CellLocation {
  /** Linear index into the raster, `row * SCAN_COLS + col`. */
  index: number;
  /** True when the cell was matched from the scenario's own multipliers.
   *  False means it was placed proportionally from the done/total counts —
   *  still real progress, but not a real coordinate. */
  exact: boolean;
}

/**
 * Where on the grid the message's `last_scenario` sits.
 *
 * Two paths, and the difference matters enough to report: matching the
 * emitted multipliers against the default axes gives the true cell; a run
 * with a custom grid matches nothing, so it falls back to placing the head at
 * its completed fraction of the sweep. The fallback still moves in step with
 * real work — it just is not a coordinate.
 */
export function locateCell(payload: Partial<ScenarioProgressPayload>): CellLocation | null {
  const scenario = asNumberRecord(payload.last_scenario);
  const demand = scenario?.["demand"];
  const capacity = scenario?.["capacity"];
  if (demand !== undefined && capacity !== undefined) {
    const col = axisIndex(SCAN_DEMAND, demand);
    const row = axisIndex(SCAN_CAPACITY, capacity);
    if (col !== null && row !== null) return { index: row * SCAN_COLS + col, exact: true };
  }

  const done = finite(payload.scenarios_done);
  const total = finite(payload.scenarios_total);
  if (done !== null && total !== null && total > 0) {
    return { index: clamp(Math.floor(((done - 1) / total) * SCAN_CELLS), 0, SCAN_CELLS - 1), exact: false };
  }
  return null;
}

export interface SweepView {
  /** The cell being evaluated. Null when nothing has been reported yet. */
  head: number | null;
  /** Whether `head` is a matched coordinate rather than a proportional one. */
  headExact: boolean;
  /** Where `best_objective` last improved. */
  bestCell: number | null;
  /** True only when the improving scenario IS the one the message reported.
   *  Progress is batched (`progress_every`, default 10), so an improvement
   *  usually happened somewhere inside the batch and `last_scenario` is
   *  merely the batch's final member. */
  bestCellExact: boolean;
  bestObjective: number | null;
  scenariosDone: number | null;
  scenariosTotal: number | null;
  percent: number | null;
  lastScenario: Record<string, number> | null;
  lastOutcome: Record<string, number> | null;
}

export const EMPTY_SWEEP: SweepView = {
  head: null,
  headExact: false,
  bestCell: null,
  bestCellExact: false,
  bestObjective: null,
  scenariosDone: null,
  scenariosTotal: null,
  percent: null,
  lastScenario: null,
  lastOutcome: null,
};

/** Rounded to 6dp model-side, so this only has to absorb float noise. */
function sameNumber(a: number, b: number): boolean {
  return Math.abs(a - b) <= 1e-6 * Math.max(1, Math.abs(a), Math.abs(b));
}

/**
 * Fold the whole progress history into one frame.
 *
 * Folded rather than read off the latest message because `best_objective` is
 * monotonic and its *improvements* are the events worth marking — the latest
 * message carries the current best but not where it was found.
 */
export function deriveSweep(progress: readonly ProgressMessage[]): SweepView {
  const view: SweepView = { ...EMPTY_SWEEP };

  for (const message of progress) {
    const payload = payloadOf<ScenarioProgressPayload>(message);
    const location = locateCell(payload);
    const outcome = asNumberRecord(payload.last_outcome);
    const metric = finite(message.primary_metric);

    if (location !== null) {
      view.head = location.index;
      view.headExact = location.exact;
    }
    if (metric !== null && (view.bestObjective === null || metric > view.bestObjective)) {
      view.bestObjective = metric;
      if (location !== null) {
        view.bestCell = location.index;
        const reported = outcome?.["objective"];
        view.bestCellExact = location.exact && reported !== undefined && sameNumber(reported, metric);
      }
    }

    view.scenariosDone = finite(payload.scenarios_done) ?? view.scenariosDone;
    view.scenariosTotal = finite(payload.scenarios_total) ?? view.scenariosTotal;
    // `percent_complete: null` is a real value on any single message; keep the
    // last one that had a number rather than blanking the readout mid-run.
    view.percent = finite(message.percent_complete) ?? view.percent;
    view.lastScenario = asNumberRecord(payload.last_scenario) ?? view.lastScenario;
    view.lastOutcome = outcome ?? view.lastOutcome;
  }

  return view;
}

export interface ObjectivePoint {
  scenario_index: number;
  objective: number;
}

/**
 * The completion chart's series, from `result` previews.
 *
 * `ScenarioModel.preview_axes = ("scenario_index", "objective")`, so the
 * server's LTTB pass keeps whole rows chosen on exactly these two columns —
 * no interpolation, just fewer of the real points.
 *
 * Keyed by `scenario_index` so a scenario cannot be plotted twice. The store
 * dedupes messages by `seq`; this dedupes rows, which is the case a retried
 * or re-emitted chunk would produce.
 */
export function objectivePoints(results: readonly ResultMessage[]): ObjectivePoint[] {
  const byIndex = new Map<number, number>();
  for (const result of results) {
    for (const row of result.preview) {
      const index = finite(row["scenario_index"]);
      const objective = finite(row["objective"]);
      if (index !== null && objective !== null) byIndex.set(index, objective);
    }
  }
  return [...byIndex.entries()]
    .map(([scenario_index, objective]) => ({ scenario_index, objective }))
    .sort((a, b) => a.scenario_index - b.scenario_index);
}
