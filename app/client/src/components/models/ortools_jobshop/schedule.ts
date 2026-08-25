/**
 * What the Gantt draws: the shop floor's shape while the run is live, and the
 * real schedule once result rows exist.
 *
 * ## Why the shape is read out of the LOG stream
 *
 * A `ModelViewProps` carries `state` and a `RunSnapshot` — not the run's
 * config. Progress payloads carry `n_jobs` / `n_machines` / `n_operations`, so
 * the *counts* are available from the first sample; what they do not carry is
 * the machine NAMES, the makespan lower bound, or the deadline. Those are in
 * the model's own `input`/`build` phase logging:
 *
 *     60 jobs / 247 operations over 5 machines (mix, rest, bake, decorate,
 *     pack); 4210 machine-minutes of work, lower bound on makespan 1100 min;
 *     the jobs stand for 1203 sales transactions
 *
 *     built: 247 interval variables, 5 no-overlap constraints, horizon 4210
 *     min, deadline 900 min. CP-SAT has no variable or constraint cap; ...
 *
 * Parsing those is fragile in exactly one way — the model can reword them —
 * and the failure mode is benign: the machine lanes fall back to
 * `instance.py::STAGES` and the view says the shape is a default. The
 * alternative, hardcoding the numbers, would draw a *wrong* floor with total
 * confidence.
 *
 * The deadline is the one worth the regex on its own. It is the only route to
 * INFEASIBLE in this model — a job shop with an open horizon can always run
 * its jobs end to end — so an infeasible run's explanation is a user input,
 * and the view can only say so if it read the number.
 *
 * ## Why the settled Gantt can be partial
 *
 * `result.preview` is downsampled server-side (`job/emitter.py`, 500 points)
 * while `row_count` is the durable truth. A default 60-job run is ~250
 * operations, so the preview is normally complete — but a 400-job run is
 * ~1700, and a Gantt silently missing two thirds of its operations is a lie
 * about a schedule. Hence `truncated`, and hence the utilisation figures are
 * suppressed when it is set: busy-minutes over a sampled subset understates
 * every machine by an unknown amount.
 */

import type { LogMessage, ResultMessage } from "@/lib/envelope";

/** `job/models/ortools_jobshop/instance.py::STAGES`. The index into this tuple IS
 *  the `machine_id` on the result rows. Used only as the fallback lane naming
 *  when the build log has not arrived — never to relabel rows that carry their
 *  own `machine_label`. */
export const STAGES = ["mix", "rest", "bake", "decorate", "pack"] as const;

/** `job/models/ortools_jobshop/model.py::DEFAULT_MAX_JOBS`. */
const DEFAULT_JOBS = 60;

export interface InstanceShape {
  jobs: number | null;
  operations: number | null;
  machines: number;
  machineNames: readonly string[];
  totalMinutes: number | null;
  /** The trivial bound: no schedule beats the longest job or the busiest
   *  machine. The single most useful number to put next to an INFEASIBLE,
   *  because it is what the deadline has to clear. */
  makespanLowerBound: number | null;
  /** How many real sales rows stand behind the scheduled batches. */
  transactions: number | null;
  horizon: number | null;
  /** The only constraint that can make this model INFEASIBLE. */
  deadlineMinutes: number | null;
  /** Where the numbers came from, so the UI can say "this is a placeholder"
   *  rather than implying an illustrative floor is the run's real one. */
  source: "log" | "payload" | "default";
}

/** Counts as the progress payload reports them. Structurally satisfied by a
 *  `JobshopPoint`, so the caller can hand one straight over without this
 *  module depending on the series. */
export interface InstanceCounts {
  nJobs: number | null;
  nMachines: number | null;
  nOperations: number | null;
}

const SHAPE_LINE =
  /(\d+)\s+jobs\s*\/\s*(\d+)\s+operations\s+over\s+(\d+)\s+machines\s*\(([^)]*)\)/i;
const MACHINE_MINUTES = /(\d+(?:\.\d+)?)\s+machine-minutes\s+of\s+work/i;
const LOWER_BOUND = /lower\s+bound\s+on\s+makespan\s+(\d+(?:\.\d+)?)\s*min/i;
const TRANSACTIONS = /stand\s+for\s+(\d+)\s+sales\s+transactions/i;
const BUILT_LINE =
  /built:\s*(\d+)\s+interval\s+variables,\s*(\d+)\s+no-overlap\s+constraints,\s*horizon\s+(\d+)\s*min(?:,\s*deadline\s+(\d+)\s*min)?/i;

function positive(value: string | undefined): number | null {
  if (value === undefined) return null;
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : null;
}

/**
 * The shop floor, from the logs and the latest progress counts.
 *
 * The counts win where both exist: the payload restates them on every sample
 * and cannot be reworded, while the log line is prose. The log is the only
 * source for everything else.
 */
export function resolveInstance(
  logs: readonly LogMessage[],
  counts: InstanceCounts | null,
): InstanceShape {
  let jobs: number | null = null;
  let operations: number | null = null;
  let machines: number | null = null;
  let machineNames: string[] | null = null;
  let totalMinutes: number | null = null;
  let makespanLowerBound: number | null = null;
  let transactions: number | null = null;
  let horizon: number | null = null;
  let deadlineMinutes: number | null = null;

  // Flags rather than null-checks on the values: a shape line reporting zero
  // jobs (a run with an empty shop floor, which the model logs and treats as a
  // non-error) is still the line to use, and must not send the scan looking
  // further back for an older run's numbers.
  let shapeSeen = false;
  let builtSeen = false;

  // Last match wins: a re-run within one page session appends, and the most
  // recent build line describes the run being watched.
  for (let i = logs.length - 1; i >= 0; i -= 1) {
    const message = logs[i]?.message ?? "";
    if (!shapeSeen) {
      const match = SHAPE_LINE.exec(message);
      if (match) {
        shapeSeen = true;
        jobs = positive(match[1]);
        operations = positive(match[2]);
        machines = positive(match[3]);
        const names = (match[4] ?? "")
          .split(",")
          .map((name) => name.trim())
          .filter((name) => name.length > 0);
        machineNames = names.length > 0 ? names : null;
        totalMinutes = positive(MACHINE_MINUTES.exec(message)?.[1]);
        makespanLowerBound = positive(LOWER_BOUND.exec(message)?.[1]);
        transactions = positive(TRANSACTIONS.exec(message)?.[1]);
      }
    }
    if (!builtSeen) {
      const built = BUILT_LINE.exec(message);
      if (built) {
        builtSeen = true;
        horizon = positive(built[3]);
        deadlineMinutes = positive(built[4]);
      }
    }
    if (shapeSeen && builtSeen) break;
  }

  const fromLog = shapeSeen || builtSeen;
  const resolvedMachines = counts?.nMachines ?? machines;
  const source: InstanceShape["source"] = fromLog
    ? "log"
    : counts !== null && counts.nMachines !== null
      ? "payload"
      : "default";

  return {
    jobs: counts?.nJobs ?? jobs ?? (source === "default" ? DEFAULT_JOBS : null),
    operations: counts?.nOperations ?? operations,
    // A floor with no machines is not drawable, so the fallback is the model's
    // own stage list rather than zero lanes.
    machines: resolvedMachines !== null && resolvedMachines > 0 ? resolvedMachines : STAGES.length,
    machineNames: machineNames ?? [...STAGES],
    totalMinutes,
    makespanLowerBound,
    transactions,
    horizon,
    deadlineMinutes,
    source,
  };
}

/* ================================================================== *
 * The real schedule, from result rows
 * ================================================================== */

export interface ScheduledOperation {
  jobId: number | null;
  jobLabel: string | null;
  operationIndex: number | null;
  machineId: number;
  machineLabel: string | null;
  start: number;
  duration: number;
  end: number;
}

export interface ResolvedSchedule {
  operations: readonly ScheduledOperation[];
  /** Machine ids actually present in the rows, ascending. */
  machineIds: readonly number[];
  machineLabels: ReadonlyMap<number, string>;
  /** Run-level column, repeated on every row. The authoritative makespan, and
   *  NOT the same as the largest `end_minute` in a truncated preview. */
  makespan: number | null;
  bestBound: number | null;
  /** CP-SAT's own status name, off the result rows rather than the payload —
   *  which matters for a run whose final progress sample was dropped. */
  solverStatus: string | null;
  solutionsFound: number | null;
  wallTime: number | null;
  dataSource: string | null;
  dataSynthetic: boolean | null;
  /** Durable row count summed across chunks. The truth about the run. */
  rowCount: number;
  /** Rows this client actually holds. */
  previewCount: number;
  /** The preview is a sample of the durable rows: the Gantt is incomplete. */
  truncated: boolean;
  /** No result message has been seen at all. Distinct from a run that
   *  succeeded with zero rows, which is a real and reportable outcome. */
  empty: boolean;
}

const EMPTY_SCHEDULE: ResolvedSchedule = {
  operations: [],
  machineIds: [],
  machineLabels: new Map(),
  makespan: null,
  bestBound: null,
  solverStatus: null,
  solutionsFound: null,
  wallTime: null,
  dataSource: null,
  dataSynthetic: null,
  rowCount: 0,
  previewCount: 0,
  truncated: false,
  empty: true,
};

function asNumber(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  return value;
}

function asIndex(value: unknown): number | null {
  const number = asNumber(value);
  if (number === null || !Number.isInteger(number) || number < 0) return null;
  return number;
}

function asString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

/**
 * Turn `result.preview` rows into scheduled operations.
 *
 * Every field is re-checked rather than trusted: `preview` is
 * `Array<Record<string, unknown>>` on the wire, the server validates nothing
 * inside it, and one malformed row must cost that row and not the view.
 *
 * `end_minute` is present in the DDL and is always `start + duration` when the
 * model wrote it, so it is used only to RECOVER a missing duration. Deriving
 * the end from the duration otherwise keeps the bar's width and its right edge
 * from ever disagreeing.
 */
export function resolveSchedule(results: readonly ResultMessage[]): ResolvedSchedule {
  if (results.length === 0) return EMPTY_SCHEDULE;

  const operations: ScheduledOperation[] = [];
  const machineLabels = new Map<number, string>();
  let rowCount = 0;
  let previewCount = 0;
  let makespan: number | null = null;
  let bestBound: number | null = null;
  let solverStatus: string | null = null;
  let solutionsFound: number | null = null;
  let wallTime: number | null = null;
  let dataSource: string | null = null;
  let dataSynthetic: boolean | null = null;

  for (const result of results) {
    rowCount += result.row_count;
    for (const row of result.preview) {
      previewCount += 1;

      // Run-level columns are repeated on every row, so the first row that
      // carries one settles it. `??=` rather than overwrite: a malformed later
      // row must not blank a value an earlier one supplied.
      makespan ??= asNumber(row["makespan"]);
      bestBound ??= asNumber(row["best_bound"]);
      solverStatus ??= asString(row["solver_status"]);
      solutionsFound ??= asNumber(row["solutions_found"]);
      wallTime ??= asNumber(row["wall_time_seconds"]);
      dataSource ??= asString(row["data_source"]);
      if (dataSynthetic === null && typeof row["data_synthetic"] === "boolean") {
        dataSynthetic = row["data_synthetic"];
      }

      const machineId = asIndex(row["machine_id"]);
      const start = asIndex(row["start_minute"]);
      if (machineId === null || start === null) continue;

      const end = asIndex(row["end_minute"]);
      const duration = asIndex(row["duration_minutes"]) ?? (end === null ? null : end - start);
      // A zero-length operation is not something a machine does
      // (`MIN_OPERATION_MINUTES`), so a row claiming one is malformed.
      if (duration === null || duration <= 0) continue;

      const label = asString(row["machine_label"]);
      if (label !== null && !machineLabels.has(machineId)) machineLabels.set(machineId, label);

      operations.push({
        jobId: asIndex(row["job_id"]),
        jobLabel: asString(row["job_label"]),
        operationIndex: asIndex(row["operation_index"]),
        machineId,
        machineLabel: label,
        start,
        duration,
        end: start + duration,
      });
    }
  }

  return {
    operations,
    machineIds: [...new Set(operations.map((op) => op.machineId))].sort((a, b) => a - b),
    machineLabels,
    makespan,
    bestBound,
    solverStatus,
    solutionsFound,
    wallTime,
    dataSource,
    dataSynthetic,
    rowCount,
    previewCount,
    truncated: previewCount < rowCount,
    empty: false,
  };
}

/* ================================================================== *
 * Layout
 * ================================================================== */

/** The Gantt's horizontal extent, in viewBox units. The component divides by
 *  this to get percentages, so nothing downstream depends on a pixel width. */
export const PLOT_WIDTH = 1000;

/** A one-minute operation on a four-hour schedule is a quarter of a unit wide
 *  and would vanish. Widening it to something visible overstates it slightly;
 *  dropping it would understate the machine's occupancy entirely, which is
 *  worse on a chart whose whole subject is no-overlap. */
const MIN_BAR = 2;

/** Past this the floor is a texture rather than a schedule, and the browser is
 *  laying out thousands of nodes. The preview caps at 500 rows per chunk so
 *  this is not normally reachable, and whatever it drops is COUNTED. */
const MAX_BARS = 1200;

export interface GanttBar {
  key: string;
  lane: number;
  /** viewBox units from the left edge, 0..PLOT_WIDTH. */
  x: number;
  width: number;
  operation: ScheduledOperation;
}

export interface GanttLane {
  machineId: number;
  label: string;
  bars: readonly GanttBar[];
  /** Minutes this machine is occupied across the bars SHOWN. Understated
   *  whenever the preview is truncated, which is why the view suppresses the
   *  derived utilisation in that case rather than printing a smaller number. */
  busyMinutes: number;
}

export interface GanttLayout {
  lanes: readonly GanttLane[];
  /** Minutes the full width represents. Never zero — a degenerate schedule
   *  falls back to 1 so nothing downstream divides by nothing. */
  span: number;
  /** True when `span` came from the run-level `makespan` rather than from the
   *  largest end minute among the bars. On a truncated preview those differ,
   *  and the makespan is the honest axis. */
  spanFromMakespan: boolean;
  barCount: number;
  /** Operations dropped by `MAX_BARS`. Reported, never silently discarded. */
  hidden: number;
}

/**
 * Place scheduled operations into machine lanes.
 *
 * The span deliberately prefers the run-level `makespan` over the largest end
 * minute present. They are the same number on a complete preview; on a
 * truncated one the largest end is smaller, and fitting the axis to it would
 * stretch a sampled subset across the full width and make a partial schedule
 * look like a full one.
 */
export function layoutGantt(
  schedule: ResolvedSchedule,
  shape: { machines: number; machineNames: readonly string[] },
): GanttLayout {
  const maxEnd = schedule.operations.reduce((max, op) => Math.max(max, op.end), 0);
  const makespan = schedule.makespan;
  const spanFromMakespan = makespan !== null && makespan > maxEnd;
  const span = Math.max(1, spanFromMakespan && makespan !== null ? makespan : maxEnd);

  // Lanes come from the union of "machines this instance declared" and
  // "machine ids the rows actually used" — a row referencing a machine the
  // shape did not mention must not be dropped on the floor.
  const highestId = schedule.machineIds.reduce((max, id) => Math.max(max, id), -1);
  const laneCount = Math.max(shape.machines, highestId + 1);

  const lanes: GanttLane[] = Array.from({ length: laneCount }, (_, machineId) => ({
    machineId,
    label:
      schedule.machineLabels.get(machineId) ??
      shape.machineNames[machineId] ??
      `machine-${machineId}`,
    bars: [],
    busyMinutes: 0,
  }));

  // Sorted so the cap below drops a deterministic tail rather than whichever
  // rows the preview sampler happened to return last.
  const ordered = [...schedule.operations].sort(
    (a, b) => a.machineId - b.machineId || a.start - b.start,
  );
  const shown = ordered.slice(0, MAX_BARS);

  for (const [index, operation] of shown.entries()) {
    const lane = lanes[operation.machineId];
    if (lane === undefined) continue;
    const rawX = (operation.start / span) * PLOT_WIDTH;
    const rawWidth = (operation.duration / span) * PLOT_WIDTH;
    const width = Math.max(MIN_BAR, rawWidth);
    // Widening a hairline bar must not push it past the right edge, or the
    // last operation of the schedule would appear to overrun the makespan.
    const x = Math.min(rawX, PLOT_WIDTH - width);
    (lane.bars as GanttBar[]).push({
      key: `${operation.jobId ?? "j"}-${operation.operationIndex ?? index}-${operation.start}-${index}`,
      lane: operation.machineId,
      x: Math.max(0, x),
      width,
      operation,
    });
    lane.busyMinutes += operation.duration;
  }

  return {
    lanes,
    span,
    spanFromMakespan,
    barCount: shown.length,
    hidden: ordered.length - shown.length,
  };
}

/* ================================================================== *
 * The decorative layer
 * ================================================================== */

export interface DecorativeBar {
  lane: number;
  x: number;
  width: number;
}

/**
 * The bars shown while the run is live.
 *
 * DECORATIVE, and it cannot be otherwise: no progress message carries a single
 * operation start time. `model.py::_read_solution` reads the schedule out of
 * the solver's variables ONCE, after `solve()` returns, precisely because
 * snapshotting it inside every solution callback would cost O(operations) per
 * improvement and buy nothing — so per-operation data does not exist anywhere
 * on the wire until the run is over.
 *
 * What IS real is when this is called with a new `pulse`: once per observed
 * improvement in `solutions_found`, and CP-SAT's callback only fires on an
 * improving solution. A still floor therefore means a search that is genuinely
 * not improving, which on this solver is normal for long stretches.
 *
 * Bars within a lane never overlap, because that is the one thing the model
 * forbids (`add_no_overlap`) and drawing two operations on top of each other
 * would depict something CP-SAT would have rejected.
 *
 * Pure and deterministic: no timer, no `Math.random`. Two tabs watching the
 * same run draw the same frame, a re-render draws the same frame, and a test
 * can assert on it.
 */
export function decorativeBars(pulse: number, laneCount: number): DecorativeBar[] {
  const bars: DecorativeBar[] = [];
  if (laneCount <= 0) return bars;

  for (let lane = 0; lane < laneCount; lane += 1) {
    let cursor = hash(lane * 31 + 5, pulse) % 60;
    let step = 0;
    while (cursor < PLOT_WIDTH && step < 64) {
      const h = hash(lane * 977 + step, pulse + 1);
      const width = 14 + (h % 90);
      if (cursor + width > PLOT_WIDTH) break;
      bars.push({ lane, x: cursor, width });
      // The gap is what keeps the lane a sequence of distinct operations
      // rather than one long block; the minimum of 6 guarantees no overlap.
      cursor += width + 6 + ((h >> 7) % 70);
      step += 1;
    }
  }
  return bars;
}

/** A 32-bit integer hash. Not cryptographic and not meant to be — it needs to
 *  be stable across tabs and cheap enough to run per lane per pulse. */
function hash(a: number, b: number): number {
  let x = (a * 0x9e37_79b1) ^ (b * 0x85eb_ca6b);
  x = Math.imul(x ^ (x >>> 15), 0x2545_f491);
  x ^= x >>> 13;
  return Math.abs(x);
}
