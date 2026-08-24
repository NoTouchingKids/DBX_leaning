/**
 * Shared by BOTH Gurobi model views, and by nothing else.
 *
 * `gurobi_scheduling` and `gurobi_routing` are sampled by the same
 * `job/drivers/gurobi.py::_sample_progress`, so their progress payloads are
 * byte-identical — the models differ in what they are solving, not in what
 * they report. A second copy of this derivation next door is a copy that
 * drifts, and the two would then disagree about the same numbers.
 *
 * It lives in its own directory rather than in `gurobi_scheduling/` so that
 * neither model imports the other's internals: a sibling reaching into a
 * sibling reads as an accident, and the next person to touch scheduling has
 * no way to know routing depends on these files.
 *
 * Nothing model-specific belongs here. The labels differ between the two
 * views because the objective means different things, and that difference
 * stays in each view's own directory.
 */

/**
 * Branch-and-bound telemetry, derived from the progress stream.
 *
 * This module is shared with `gurobi_routing`. It lives in this directory
 * rather than somewhere neutral because both Gurobi models are sampled by the
 * SAME code — `job/drivers/gurobi.py::_sample_progress` — so their payloads
 * are byte-identical, and a second copy next door would be a copy that drifts.
 *
 * Four things the driver does that this file exists to absorb:
 *
 *  1. **`incumbent` and `best_bound` are `number | null`, not `number`.**
 *     Gurobi reports ±1e100 before the first feasible solution; the driver's
 *     `_real_or_none()` maps that to `None`, i.e. JSON `null`. `lib/models.ts`
 *     declares both as `number`, which is optimistic — and a chart that
 *     believes it plots `null` as 0 and drags the objective axis to the
 *     origin for the whole run. Everything here goes through `finite()`.
 *
 *  2. **`primary_metric` is the gap as a FRACTION**, `|inc - bnd| / |inc|`,
 *     and is null whenever either side is unknown or the incumbent is within
 *     1e-10 of zero. It is not a percentage and it is not monotone: the bound
 *     moves too, so a gap can widen between samples. Nothing here assumes
 *     otherwise.
 *
 *  3. **Samples are ~2s apart at best.** `progress_every_s` defaults to 2.0
 *     and the callback only fires on `MIP` events, so a long presolve or a
 *     long root relaxation produces NO progress messages at all. An empty
 *     series is a normal state, not a loading state.
 *
 *  4. **The store appends in ARRIVAL order, not seq order.** A hydrate/live
 *     overlap can interleave. Sorting by `seq` — assigned by the job,
 *     monotonic per run — is what makes this a series. `elapsed_seconds` is
 *     Gurobi's RUNTIME inside one `optimize()` call, so seq order yields
 *     non-decreasing elapsed as a consequence rather than as an assumption.
 */

import { isTerminal, type ProgressMessage, type StatusMessage } from "@/lib/envelope";
import type { GurobiProgressPayload } from "@/lib/models";
import { payloadOf } from "../contract";

export interface MipPoint {
  seq: number;
  /** Gurobi RUNTIME in seconds, the x axis of both charts. */
  elapsed: number;
  /** Best feasible objective. Null until the first feasible solution. */
  incumbent: number | null;
  /** Best proven bound. Null while Gurobi still reports the sentinel. */
  bestBound: number | null;
  /** `primary_metric * 100`. Null when the gap is not yet defined. */
  gapPercent: number | null;
  nodesExplored: number | null;
  nodesRemaining: number | null;
  /**
   * `nodesExplored` clamped to the domain a log axis can actually draw.
   * Zero and negative values become null — a hole in the line — rather than
   * being nudged to 1, which would draw a node that was never explored.
   */
  nodesLog: number | null;
  solutionCount: number | null;
  /** True on the first sample to report a HIGHER `solution_count` than any
   *  earlier sample: the observable "new incumbent" event. */
  newIncumbent: boolean;
  /** `solutionCount` on a new-incumbent sample, else null. Exists so a chart
   *  can scatter the incumbent markers as an ordinary series instead of a
   *  custom dot renderer. */
  incumbentMark: number | null;
}

/**
 * A number, or null for anything a chart cannot plot.
 *
 * `payload` is `Record<string, unknown>` on the wire, and the two fields that
 * matter most are genuinely nullable (see the header). Booleans are rejected
 * explicitly because `Number(true)` is 1, which would silently invent data.
 */
export function finite(value: unknown): number | null {
  if (typeof value !== "number") return null;
  return Number.isFinite(value) ? value : null;
}

export function deriveMipSeries(progress: readonly ProgressMessage[]): MipPoint[] {
  // Dedupe on seq before ordering: backfill and live can both deliver the
  // same message, and a duplicated sample would be counted as a second
  // incumbent event by the pulse detector below.
  const bySeq = new Map<number, ProgressMessage>();
  for (const message of progress) {
    if (!bySeq.has(message.seq)) bySeq.set(message.seq, message);
  }
  const ordered = [...bySeq.values()].sort((a, b) => a.seq - b.seq);

  let highest = 0;
  return ordered.map((message) => {
    const payload = payloadOf<GurobiProgressPayload>(message);
    const solutionCount = finite(payload.solution_count);
    // Counting strict increases counts EVENTS, not incumbents: two solutions
    // found inside one 2s sampling window arrive as a single jump, and that
    // is all the data supports claiming. `highest` only ever rises, so a
    // sample that arrives late with a stale count cannot fake a pulse.
    const newIncumbent = solutionCount !== null && solutionCount > highest;
    if (newIncumbent && solutionCount !== null) highest = solutionCount;

    const nodesExplored = finite(payload.nodes_explored);
    const gap = finite(message.primary_metric);

    return {
      seq: message.seq,
      // The envelope guarantees a float here; the fallback exists so one
      // malformed message cannot turn the whole x axis into NaN.
      elapsed: finite(message.elapsed_seconds) ?? 0,
      incumbent: finite(payload.incumbent),
      bestBound: finite(payload.best_bound),
      gapPercent: gap === null ? null : gap * 100,
      nodesExplored,
      nodesRemaining: finite(payload.nodes_remaining),
      nodesLog: nodesExplored !== null && nodesExplored >= 1 ? nodesExplored : null,
      solutionCount,
      newIncumbent,
      incumbentMark: newIncumbent ? solutionCount : null,
    };
  });
}

export interface IncumbentActivity {
  /**
   * How many new-incumbent EVENTS have been observed. This is the animation's
   * clock: one pulse per increment, and no timer anywhere.
   */
  pulses: number;
  /** Latest reported `solution_count`, or null if none has been reported. */
  solutionCount: number | null;
  /** Seq of the most recent pulse — a stable React key for "the current
   *  frame", so a re-render that changes nothing does not re-animate. */
  lastPulseSeq: number | null;
  /** Latest sample, for the readouts next to the animation. */
  latest: MipPoint | null;
}

export function incumbentActivity(points: readonly MipPoint[]): IncumbentActivity {
  let pulses = 0;
  let lastPulseSeq: number | null = null;
  let solutionCount: number | null = null;

  for (const point of points) {
    if (point.newIncumbent) {
      pulses += 1;
      lastPulseSeq = point.seq;
    }
    if (point.solutionCount !== null) solutionCount = point.solutionCount;
  }

  return { pulses, solutionCount, lastPulseSeq, latest: points.at(-1) ?? null };
}

/**
 * Whether either chart has anything to draw.
 *
 * Deliberately not `points.length > 0`: a run can emit progress samples in
 * which every numeric field is null (before the first feasible solution, with
 * the node count still at zero), and an axis fitted to nothing but nulls
 * renders as a broken frame.
 */
export function hasPlottable(
  points: readonly MipPoint[],
  keys: readonly (keyof MipPoint)[],
): boolean {
  return points.some((point) => keys.some((key) => point[key] !== null));
}

/**
 * The `detail` on the terminal status message, if the run has ended.
 *
 * `job/drivers/gurobi.py::_detail` writes Gurobi's own word here — "optimal",
 * "time limit reached", "suboptimal but feasible", "infeasible". That single
 * word is the difference between a run that closed the gap and a run that ran
 * out of clock with a usable incumbent, and neither the status nor any chart
 * carries it. Highest seq wins: statuses can arrive out of order across a
 * hydrate/live boundary.
 */
export function terminalDetail(statuses: readonly StatusMessage[]): string | null {
  const latest = statuses.reduce<StatusMessage | null>(
    (best, status) => (best === null || status.seq > best.seq ? status : best),
    null,
  );
  if (latest === null || !isTerminal(latest.status)) return null;
  return latest.detail;
}
