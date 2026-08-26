/**
 * Every number this view plots or paces itself by, derived in one place.
 *
 * All of it is pure and none of it touches React, because the interesting
 * failures in an annealing view are arithmetic, not markup: a "best" line that
 * is silently recomputed as a running max would hide a real model bug, a heat
 * value that saturates makes a cooling animation lie, and a feasibility
 * readout with the wrong tone turns the algorithm working into an alarm.
 * Those are the things `series.test.ts` pins down.
 *
 * The one rule this file exists to enforce: `current_objective` and
 * `primary_metric` are two different solutions and are never reconciled here.
 * `primary_metric` (`best_fare`) is the incumbent, monotonic because the model
 * only moves it on a feasible improvement. `current_objective` is where the
 * random walk happens to be, penalty included, and it goes down on purpose.
 * Anything that smooths, clamps or max-fills one against the other is deleting
 * the only thing this model has to say.
 */

import { payloadOf } from "@/components/models/contract";
import type { ProgressMessage, UiRunState } from "@/lib/envelope";
import type { AnnealingProgressPayload } from "@/lib/models";

/** A finite number, or null. `payloadOf` returns a `Partial`, so every field
 *  is `T | undefined` before this — and the server already sanitises NaN and
 *  ±Infinity to null on `primary_metric` but makes no such promise about
 *  `payload`, which it does not validate at all. */
function num(value: number | undefined): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** One progress message, flattened. Nulls throughout: a field the model has
 *  not emitted is genuinely absent, not zero. */
export interface AnnealingPoint {
  seq: number;
  elapsedSeconds: number;
  percentComplete: number | null;
  iteration: number | null;
  iterationsTotal: number | null;
  temperature: number | null;
  /** Non-monotonic on purpose. Value less the overweight penalty. */
  current: number | null;
  currentValue: number | null;
  currentWeight: number | null;
  capacity: number | null;
  /** `current_weight <= capacity`. False is the search escaping a full
   *  knapsack, not a fault. */
  feasible: boolean | null;
  acceptanceRate: number | null;
  /** Of the BEST selection, not the current walk. Deliberately not carried
   *  next to `currentValue` anywhere that could read as one solution. */
  itemsSelected: number | null;
  /** `primary_metric` — `best_fare`. Monotonic. */
  best: number | null;
}

/**
 * Sorted by `seq`, not left in arrival order.
 *
 * `RunStore.ingest` appends, and backfill legitimately delivers messages
 * BELOW the high-water mark when it fills an observed gap — so `snapshot
 * .progress` is chronological most of the time and not always. Recharts draws
 * a line in array order, so one late-arriving early point would put a visible
 * zigzag through the middle of the chart and read as the search doing
 * something it did not do.
 */
export function buildPoints(
  progress: readonly ProgressMessage[],
): AnnealingPoint[] {
  const ordered = progress.toSorted((a, b) => a.seq - b.seq);
  return ordered.map((message) => {
    const p = payloadOf<AnnealingProgressPayload>(message);
    return {
      seq: message.seq,
      elapsedSeconds: message.elapsed_seconds,
      percentComplete: message.percent_complete,
      iteration: num(p.iteration),
      iterationsTotal: num(p.iterations_total),
      temperature: num(p.temperature),
      current: num(p.current_objective),
      currentValue: num(p.current_value),
      currentWeight: num(p.current_weight),
      capacity: num(p.capacity),
      feasible: typeof p.feasible === "boolean" ? p.feasible : null,
      acceptanceRate: num(p.acceptance_rate),
      itemsSelected: num(p.items_selected),
      best: message.primary_metric,
    };
  });
}

/* ================================================================== *
 * The current-vs-best trace
 * ================================================================== */

export interface TracePoint {
  iteration: number;
  /** Null where the model did not report one; the line breaks rather than
   *  dropping to the floor. */
  current: number | null;
  best: number | null;
  /** Same y as `current`, populated ONLY where the walk is over the shift.
   *  A separate key so the over-capacity points can be marked without a
   *  custom dot renderer — and marked neutrally. */
  currentOverShift: number | null;
}

/**
 * Points keyed on `iteration`.
 *
 * Iteration, not elapsed seconds: the x axis a reader compares against is the
 * plan (`iterations_total`), and a progress message that arrived on the
 * wall-clock timer rather than the batch boundary lands between iterations
 * perfectly well. A message with no usable iteration is dropped — it has no
 * payload, so it has no `current_objective` to plot either.
 */
export function traceSeries(points: readonly AnnealingPoint[]): TracePoint[] {
  const out: TracePoint[] = [];
  for (const point of points) {
    if (point.iteration === null) continue;
    if (point.current === null && point.best === null) continue;
    out.push({
      iteration: point.iteration,
      current: point.current,
      best: point.best,
      currentOverShift: point.feasible === false ? point.current : null,
    });
  }
  return out;
}

/**
 * An x domain that survives a single point.
 *
 * A default run emits ~30 progress messages, so the first one arrives with a
 * chart already on screen. Recharts given `dataMin === dataMax` collapses the
 * axis and pins the dot to the left edge, which reads as a broken chart rather
 * than as one observation.
 */
export function traceDomain(
  points: readonly TracePoint[],
): [number, number] | null {
  if (points.length === 0) return null;
  let min = Infinity;
  let max = -Infinity;
  for (const point of points) {
    if (point.iteration < min) min = point.iteration;
    if (point.iteration > max) max = point.iteration;
  }
  if (min === max) {
    const pad = Math.max(1, Math.abs(min) * 0.05);
    return [min - pad, max + pad];
  }
  return [min, max];
}

/* ================================================================== *
 * The cooling schedule
 * ================================================================== */

export interface CoolingPoint {
  iteration: number;
  /** Strictly positive — the chart plots it on a log axis, where the model's
   *  geometric schedule is a straight line and a schedule that is not
   *  geometric is immediately visible. */
  temperature: number;
  acceptanceRate: number | null;
}

export function coolingSeries(
  points: readonly AnnealingPoint[],
): CoolingPoint[] {
  const out: CoolingPoint[] = [];
  for (const point of points) {
    if (point.iteration === null) continue;
    if (point.temperature === null || point.temperature <= 0) continue;
    out.push({
      iteration: point.iteration,
      temperature: point.temperature,
      acceptanceRate: point.acceptanceRate,
    });
  }
  return out;
}

/* ================================================================== *
 * Heat — how far through its cooling schedule the search is
 * ================================================================== */

/**
 * Where the search sits on its own cooling schedule, 1 (start) to 0 (end).
 *
 * Null when no temperature has been reported yet, which is a different thing
 * from cold and must not render as one.
 *
 * The model cools geometrically (`AnnealingModel.temperature`), so `log T` is
 * exactly linear in the iteration count from `start_temperature` at iteration
 * 0 to `end_temperature` at the last planned iteration. Two observed points
 * therefore determine the whole line, including the end temperature — which is
 * the number needed to normalise and the one number the payload never carries.
 * Fitting it beats hardcoding `END_TEMPERATURE_RATIO`, because both bounds are
 * config-overridable and derived from the fare distribution when they are not.
 */
export function deriveHeat(points: readonly AnnealingPoint[]): number | null {
  const usable = points.filter(
    (p): p is AnnealingPoint & { iteration: number; temperature: number } =>
      p.iteration !== null && p.temperature !== null && p.temperature > 0,
  );
  const first = usable[0];
  const last = usable.at(-1);
  if (first === undefined || last === undefined) return null;

  const lnNow = Math.log(last.temperature);
  const lnFirst = Math.log(first.temperature);

  // One observation, or two at the same iteration: the schedule is not yet
  // determined, and the first thing annealing does is start hot.
  if (last.iteration === first.iteration) return 1;

  const slope = (lnNow - lnFirst) / (last.iteration - first.iteration);
  // A flat or rising schedule is not a cooling schedule. Reporting "hot
  // forever" is the honest reading; pretending to a normalisation that has no
  // endpoint is not.
  if (!Number.isFinite(slope) || slope >= 0) return 1;

  const total = last.iterationsTotal;
  const endIteration = total !== null && total > 1 ? total - 1 : last.iteration;
  const lnEnd = lnFirst + slope * (endIteration - first.iteration);
  const lnStart = lnFirst + slope * (0 - first.iteration);
  const span = lnStart - lnEnd;
  if (!(span > 0)) return 1;

  return clamp01((lnNow - lnEnd) / span);
}

function clamp01(value: number): number {
  if (!Number.isFinite(value)) return 1;
  return Math.min(1, Math.max(0, value));
}

export type HeatPhase = "unknown" | "hot" | "cooling" | "cold";

export function heatPhase(heat: number | null): HeatPhase {
  if (heat === null) return "unknown";
  if (heat >= 0.62) return "hot";
  if (heat >= 0.22) return "cooling";
  return "cold";
}

/** What the phase means, in the terms a reader of this model needs: how
 *  willing the search still is to accept a move that makes things worse. */
export const HEAT_PHASE_TEXT: Record<HeatPhase, string> = {
  unknown: "no temperature reported yet",
  hot: "hot — uphill moves accepted freely",
  cooling: "cooling — fewer uphill moves accepted",
  cold: "cold — effectively hill-climbing now",
};

/* ================================================================== *
 * Shift usage — where `feasible: false` gets its tone
 * ================================================================== */

/**
 * Deliberately narrow. `bad` and `warn` are not members, so styling an
 * over-capacity walk as a fault is a type error rather than a judgement call
 * someone makes again in six months.
 */
export type CalmTone = "neutral" | "info";

export interface ShiftUsage {
  weight: number;
  capacity: number;
  /** `current_weight > capacity`. The search crosses the boundary on purpose:
   *  the model prices overweight rather than forbidding it, precisely so a
   *  locally-full knapsack is escapable. */
  overShift: boolean;
  overBy: number;
  tone: CalmTone;
  label: string;
  note: string;
}

export function shiftUsage(point: AnnealingPoint | null): ShiftUsage | null {
  if (point === null) return null;
  const { currentWeight: weight, capacity } = point;
  if (weight === null || capacity === null || capacity <= 0) return null;

  const overShift = weight > capacity;
  const overBy = Math.max(0, weight - capacity);
  return {
    weight,
    capacity,
    overShift,
    overBy,
    // Both branches are calm by construction. Over the shift is the algorithm
    // exploring, and the incumbent it will report is feasible regardless — the
    // model only ever moves `best_value` on a feasible state.
    tone: overShift ? "info" : "neutral",
    label: `${Math.round(weight)} / ${Math.round(capacity)} min`,
    note: overShift
      ? `over the shift by ${Math.round(overBy)} min — the search crosses the boundary on purpose; the kept solution is always feasible`
      : "within the shift",
  };
}

/* ================================================================== *
 * Empty states
 * ================================================================== */

/**
 * Why a chart has nothing in it. A settled run with no progress is a finished
 * story; a running one is a chart that is about to fill. Rendering the same
 * sentence for both makes the first look like it is still loading.
 */
export function emptyProgressReason(state: UiRunState | null): string {
  if (state === null) return "No run selected yet.";
  if (
    state === "SUCCEEDED" ||
    state === "FAILED" ||
    state === "CANCELLED" ||
    state === "INFEASIBLE"
  ) {
    return "This run finished without reporting any progress. The search reports every 1,000 iterations by default, so a run that stopped inside its first batch has nothing to plot.";
  }
  if (state === "QUEUED" || state === "STARTING") {
    return "Waiting for the job to start. Nothing is plotted until the search reports its first batch.";
  }
  return "No progress reported yet. The search reports every 1,000 iterations by default — about 30 points in a 30,000-iteration run.";
}
