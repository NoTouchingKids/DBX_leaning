/**
 * CP-SAT solve telemetry, derived from the progress stream.
 *
 * ## Why this is not `gurobi_shared/mipSeries.ts`
 *
 * The two are the same *shape* and almost none of the same *fields*. The
 * Gurobi driver emits `solution_count`, `nodes_explored` and `nodes_remaining`;
 * this model emits `solutions_found`, and `conflicts`/`branches` only when
 * CP-SAT hands them over. Pointing `deriveMipSeries` at a jobshop payload would
 * not fail — it would read four keys that are not there, get `undefined` for
 * each, and draw an empty chart with confident axis labels. The overlap is
 * `incumbent` / `best_bound` / the gap, and those three are not enough shared
 * surface to justify a shared module that would then have to know which model
 * it was looking at.
 *
 * Four facts about this stream that this file exists to absorb:
 *
 *  1. **`incumbent` and `best_bound` are `number | null`.** CP-SAT's
 *     pre-search bound is a real infinity (not Gurobi's ±1e100 sentinel);
 *     `model.py::_finite` maps it to `None`, which arrives as JSON null. A
 *     chart that plots null as 0 drags the objective axis to the origin for
 *     the whole pre-feasible stretch. Everything here goes through `finite()`.
 *
 *  2. **`conflicts` and `branches` are ABSENT, not null.** `_emit_progress`
 *     only writes them when the solution callback handed them over, so the
 *     final sample — emitted after the solve returns, with no callback in
 *     hand — carries neither. `payloadOf` returns a `Partial`, so absent reads
 *     as `undefined` and `finite()` turns both cases into the same null. The
 *     hole at the end of the conflicts line is real and is not bridged.
 *
 *  3. **The callback fires only on an IMPROVING solution.** Two to twenty
 *     times across a whole run, against a MIP callback's constant chatter. So
 *     `solutions_found` increments are rare and each one means something, and
 *     the sampling throttle can fold several improvements into one observed
 *     increment. `improvements` therefore counts observed EVENTS, which is all
 *     the stream supports claiming; `solutionsFound` carries CP-SAT's own
 *     exact count alongside it.
 *
 *  4. **The store appends in ARRIVAL order, not seq order.** A hydrate/live
 *     overlap can interleave, and backfill legitimately delivers messages
 *     below the high-water mark. Sorting by `seq` — assigned by the job,
 *     monotonic per run — is what makes this a series rather than a bag.
 */

import { isTerminal, type ProgressMessage, type StatusMessage } from "@/lib/envelope";
import type { JobshopProgressPayload } from "@/lib/models";
import { payloadOf } from "../contract";

export interface JobshopPoint {
  seq: number;
  /** CP-SAT wall time in seconds — the x axis of both charts. */
  elapsed: number;
  /** Best makespan found, in minutes. Null before the first feasible schedule. */
  incumbent: number | null;
  /** Best proven bound. Null while CP-SAT still reports an infinite one. */
  bestBound: number | null;
  /** `primary_metric * 100`. The model labels it `relative_gap`, NOT `mip_gap`
   *  — same formula as the Gurobi driver, but this is not a MIP. */
  gapPercent: number | null;
  /** CP-SAT's own count of improving solutions so far. */
  solutionsFound: number | null;
  /** Absent on the final sample and whenever the callback did not supply it. */
  conflicts: number | null;
  branches: number | null;
  /**
   * `conflicts` clamped to what a log axis can draw. Zero becomes null — a
   * hole in the line — rather than being nudged to 1, which would draw a
   * conflict the solver never had.
   */
  conflictsLog: number | null;
  /**
   * The envelope's `percent_complete`. Real on this model, unlike the Gurobi
   * ones' permanent null — but a TIME fraction, which is why `percentBasis`
   * travels next to it and why nothing here calls it "progress".
   */
  percentComplete: number | null;
  /** `payload.percent_complete_basis`, the model naming its own denominator. */
  percentBasis: string | null;
  /** The unconditional post-solve sample. At most one per run. */
  final: boolean;
  /** CP-SAT's own status name — OPTIMAL / FEASIBLE / INFEASIBLE /
   *  MODEL_INVALID / UNKNOWN. Only on the final sample, and NOT a `RunStatus`. */
  solverStatus: string | null;
  nJobs: number | null;
  nMachines: number | null;
  nOperations: number | null;
  /** True on the first sample reporting a HIGHER `solutions_found` than any
   *  earlier one: the observable "CP-SAT improved" event. */
  newSolution: boolean;
  /** `solutionsFound` on a new-solution sample, else null. Exists so a chart
   *  can scatter the improvement marks as an ordinary series rather than
   *  through a custom dot renderer. */
  solutionMark: number | null;
}

/**
 * A number, or null for anything a chart cannot plot.
 *
 * `payload` is `Record<string, unknown>` on the wire and the server validates
 * nothing inside it. Booleans are rejected explicitly because `Number(true)`
 * is 1, which would invent data out of `final: true`.
 */
export function finite(value: unknown): number | null {
  if (typeof value !== "number") return null;
  return Number.isFinite(value) ? value : null;
}

function text(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

export function deriveJobshopSeries(progress: readonly ProgressMessage[]): JobshopPoint[] {
  // Dedupe on seq before ordering: backfill and live can both deliver the same
  // message, and a duplicate would be counted as a second improvement below.
  const bySeq = new Map<number, ProgressMessage>();
  for (const message of progress) {
    if (!bySeq.has(message.seq)) bySeq.set(message.seq, message);
  }
  const ordered = [...bySeq.values()].sort((a, b) => a.seq - b.seq);

  let highest = 0;
  return ordered.map((message) => {
    const payload = payloadOf<JobshopProgressPayload>(message);
    const solutionsFound = finite(payload.solutions_found);
    // `highest` only ever rises, so a sample arriving late with a stale count
    // cannot fake an improvement.
    const newSolution = solutionsFound !== null && solutionsFound > highest;
    if (newSolution && solutionsFound !== null) highest = solutionsFound;

    const conflicts = finite(payload.conflicts);
    const gap = finite(message.primary_metric);

    return {
      seq: message.seq,
      // The envelope guarantees a float here; `wall_time` is the same number
      // rounded, and is the fallback so one malformed message cannot turn the
      // whole x axis into NaN.
      elapsed: finite(message.elapsed_seconds) ?? finite(payload.wall_time) ?? 0,
      incumbent: finite(payload.incumbent),
      bestBound: finite(payload.best_bound),
      gapPercent: gap === null ? null : gap * 100,
      solutionsFound,
      conflicts,
      branches: finite(payload.branches),
      conflictsLog: conflicts !== null && conflicts >= 1 ? conflicts : null,
      percentComplete: finite(message.percent_complete),
      percentBasis: text(payload.percent_complete_basis),
      final: payload.final === true,
      solverStatus: text(payload.solver_status),
      nJobs: finite(payload.n_jobs),
      nMachines: finite(payload.n_machines),
      nOperations: finite(payload.n_operations),
      newSolution,
      solutionMark: newSolution ? solutionsFound : null,
    };
  });
}

export interface SolveActivity {
  /**
   * How many improvement EVENTS have been observed. This is the animation's
   * clock: one redraw per event, and no timer anywhere.
   *
   * Not the same as `solutionsFound`. The model always reports its first
   * solution and then throttles to one sample per `progress_every_s`, so a
   * portfolio search that improved four times inside one window arrives as a
   * single observed event. Pacing on the raw delta would fire four frames for
   * one message.
   */
  improvements: number;
  /** CP-SAT's exact count, or null if nothing has reported one. */
  solutionsFound: number | null;
  /** Seq of the most recent improvement — a stable React key for "the current
   *  frame", so a re-render that changes nothing does not re-animate. */
  lastImprovementSeq: number | null;
  /** Latest sample, for the readouts next to the animation. */
  latest: JobshopPoint | null;
  /** Highest-seq sample that carried a `solver_status`, i.e. the final one.
   *  Kept separate from `latest` because nothing guarantees the final sample
   *  is the last thing the store holds after a hydrate/live overlap. */
  solverStatus: string | null;
}

export function solveActivity(points: readonly JobshopPoint[]): SolveActivity {
  let improvements = 0;
  let lastImprovementSeq: number | null = null;
  let solutionsFound: number | null = null;
  let solverStatus: string | null = null;

  for (const point of points) {
    if (point.newSolution) {
      improvements += 1;
      lastImprovementSeq = point.seq;
    }
    if (point.solutionsFound !== null) solutionsFound = point.solutionsFound;
    if (point.solverStatus !== null) solverStatus = point.solverStatus;
  }

  return {
    improvements,
    solutionsFound,
    lastImprovementSeq,
    latest: points.at(-1) ?? null,
    solverStatus,
  };
}

export interface SolverClock {
  /** 0..100, or null when the model had no honest denominator. */
  percent: number | null;
  /** The model's own name for the denominator, verbatim off the wire. */
  basis: string | null;
}

/**
 * The `percent_complete` reading, kept behind a name that cannot be mistaken
 * for search progress.
 *
 * `percent_complete` is genuinely populated on this model — unlike the two
 * Gurobi ones, where it is permanently null — and that makes it *more*
 * dangerous, not less: it is elapsed solver time against `max_time_in_seconds`,
 * so 90% means the clock is nearly up and says nothing at all about how much
 * search is left. With no time limit configured there is no denominator and
 * the field is null until the final sample, which reports 100 because "this
 * search terminated" is knowable without a budget.
 */
export function solverClock(points: readonly JobshopPoint[]): SolverClock {
  let percent: number | null = null;
  let basis: string | null = null;
  for (const point of points) {
    if (point.percentComplete !== null) percent = point.percentComplete;
    if (point.percentBasis !== null) basis = point.percentBasis;
  }
  return { percent, basis };
}

/**
 * Whether a chart has anything to draw.
 *
 * Deliberately not `points.length > 0`: an INFEASIBLE run emits exactly one
 * sample and every numeric field on it is null, and an axis fitted to nothing
 * but nulls renders as a broken frame.
 */
export function hasPlottable(
  points: readonly JobshopPoint[],
  keys: readonly (keyof JobshopPoint)[],
): boolean {
  return points.some((point) => keys.some((key) => point[key] !== null));
}

/**
 * The `detail` on the terminal status message, if the run has ended.
 *
 * `job/drivers/self_driving.py` turns whatever `run()` returned into either a
 * status ("INFEASIBLE", "FAILED" — both real `RunStatus` members) or, for any
 * other string, the `detail` on a SUCCEEDED run. That is how "optimal:
 * makespan 412 min" and "feasible: makespan 430 min, gap 4.2%" reach the
 * record, and neither the status nor any chart carries it otherwise. Highest
 * seq wins: statuses can arrive out of order across a hydrate/live boundary.
 */
export function terminalDetail(statuses: readonly StatusMessage[]): string | null {
  const latest = statuses.reduce<StatusMessage | null>(
    (best, status) => (best === null || status.seq > best.seq ? status : best),
    null,
  );
  if (latest === null || !isTerminal(latest.status)) return null;
  return latest.detail;
}
