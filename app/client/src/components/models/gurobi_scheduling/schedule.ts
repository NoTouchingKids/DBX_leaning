/**
 * What the schedule grid draws: its shape while the run is live, and its real
 * contents once results exist.
 *
 * ## Why the shape comes out of the LOG stream
 *
 * A `ModelViewProps` carries `state` and a `RunSnapshot` — not the run's
 * config. So the only in-band statement of how many staff and how many days
 * this run is solving for is the model's own `input`/`build` phase logging:
 *
 *     building: 20 staff x 14 days x 3 shifts
 *     coverage: 236 staff-shifts over 14 days, from hourly_trip_demand
 *
 * Parsing those is fragile in exactly one way — the model can reword them —
 * and the failure mode is benign: `DEFAULT_SHAPE` renders a plainly labelled
 * illustrative grid instead. The alternative, hardcoding 20 x 14, would show
 * a *wrong* grid with total confidence, which is worse. Both lines are
 * emitted before the solve starts, so in practice the grid is the run's real
 * dimensions from the first second of RUNNING.
 *
 * ## Why the succeeded grid can be partial
 *
 * `result.preview` is downsampled (`job/emitter.py`, 500 points, LTTB or an
 * evenly spaced sample) while `row_count` is the durable truth. A default
 * scheduling run assigns at most staff x days rows — 280 for the defaults, so
 * the preview is normally complete — but "normally" is not "always", and a
 * grid missing 40% of its shifts with no note is a lie. Hence `truncated`.
 */

import type { LogMessage, ResultMessage } from "@/lib/envelope";

export interface ScheduleShape {
  staffCount: number;
  days: number;
  shifts: number;
  /** Where the numbers came from. The UI says so rather than implying the
   *  illustrative default is the run's real instance. */
  source: "log" | "default";
}

/** Only reached when the build log has not arrived (QUEUED, STARTING, or a
 *  run whose logs were never backfilled). Matches the model's own defaults so
 *  the placeholder is at least plausible. */
export const DEFAULT_SHAPE: ScheduleShape = {
  staffCount: 20,
  days: 14,
  shifts: 3,
  source: "default",
};

const BUILD_LINE = /building:\s*(\d+)\s*staff\s*x\s*(\d+)\s*days\s*x\s*(\d+)\s*shifts/i;
const COVERAGE_LINE =
  /coverage:\s*(\d+(?:\.\d+)?)\s*staff-shifts\s*over\s*(\d+)\s*days,\s*from\s*([^,\n]+)/i;

export function parseScheduleShape(logs: readonly LogMessage[]): ScheduleShape {
  // Last match wins: a re-run within one page session appends, and the most
  // recent build line is the one describing the run being watched.
  for (let i = logs.length - 1; i >= 0; i -= 1) {
    const match = BUILD_LINE.exec(logs[i]?.message ?? "");
    if (!match) continue;
    const staffCount = Number(match[1]);
    const days = Number(match[2]);
    const shifts = Number(match[3]);
    if (staffCount > 0 && days > 0 && shifts > 0) {
      return { staffCount, days, shifts, source: "log" };
    }
  }
  return DEFAULT_SHAPE;
}

export interface Coverage {
  /** Staff-shifts the demand curve requires over the horizon. */
  totalDemand: number;
  days: number;
  /** e.g. `hourly_trip_demand` or the synthetic fallback's name. */
  derivedFrom: string;
  /** The model logs this line at WARNING when demand was clipped to the
   *  workforce's capacity — the single most useful thing to show next to an
   *  INFEASIBLE, because it is the constraint that was already binding. */
  clipped: boolean;
}

export function parseCoverage(logs: readonly LogMessage[]): Coverage | null {
  for (let i = logs.length - 1; i >= 0; i -= 1) {
    const message = logs[i]?.message ?? "";
    const match = COVERAGE_LINE.exec(message);
    if (!match) continue;
    const totalDemand = Number(match[1]);
    const days = Number(match[2]);
    if (!Number.isFinite(totalDemand) || !Number.isFinite(days)) continue;
    return {
      totalDemand,
      days,
      derivedFrom: (match[3] ?? "").trim(),
      clipped: /clipped to workforce capacity/i.test(message),
    };
  }
  return null;
}

/* ================================================================== *
 * The real schedule, from result rows
 * ================================================================== */

/** The canonical order from `job/models/gurobi_scheduling/instance.py::SHIFTS`.
 *  Anything else a future instance emits is kept and sorted after these,
 *  rather than dropped. */
export const SHIFT_ORDER = ["morning", "evening", "night"] as const;

export interface Assignment {
  staff: string;
  /** Zero-based day index, as the model emits it. */
  day: number;
  shift: string;
  /** `preference > 0` for this staff/shift pair. Worth showing: a schedule
   *  that meets demand while honouring preferences is the good outcome. */
  preferred: boolean;
}

export interface ResolvedSchedule {
  assignments: readonly Assignment[];
  /** Staff ids actually present in the rows, in the model's own sort order. */
  staff: readonly string[];
  /** Day indices 0..max, contiguous — an unassigned day is still a column. */
  days: readonly number[];
  shifts: readonly string[];
  /** Durable row count summed across chunks. The truth about the run. */
  rowCount: number;
  /** Rows this client actually holds. */
  previewCount: number;
  /** Preview is a sample of the durable rows: the grid is incomplete. */
  truncated: boolean;
  /** No result message has been seen at all. Distinct from a run that
   *  succeeded with zero rows, which is a real and reportable outcome. */
  empty: boolean;
}

const EMPTY_SCHEDULE: ResolvedSchedule = {
  assignments: [],
  staff: [],
  days: [],
  shifts: [],
  rowCount: 0,
  previewCount: 0,
  truncated: false,
  empty: true,
};

function asString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function asIndex(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isInteger(value) || value < 0) return null;
  return value;
}

/**
 * Turn `result.preview` rows into a grid.
 *
 * Every field is re-checked rather than trusted: `preview` is
 * `Array<Record<string, unknown>>` on the wire, the server validates nothing
 * inside it, and one malformed row must cost that row, not the view.
 */
export function resolveSchedule(results: readonly ResultMessage[]): ResolvedSchedule {
  if (results.length === 0) return EMPTY_SCHEDULE;

  const assignments: Assignment[] = [];
  let rowCount = 0;
  let previewCount = 0;

  for (const result of results) {
    rowCount += result.row_count;
    for (const row of result.preview) {
      previewCount += 1;
      const staff = asString(row["staff"]);
      const day = asIndex(row["day"]);
      const shift = asString(row["shift"]);
      if (staff === null || day === null || shift === null) continue;
      assignments.push({ staff, day, shift, preferred: row["preferred"] === true });
    }
  }

  const staff = [...new Set(assignments.map((a) => a.staff))].sort();
  const maxDay = assignments.reduce((max, a) => Math.max(max, a.day), -1);
  const shiftsSeen = [...new Set(assignments.map((a) => a.shift))].sort((a, b) => {
    const ia = SHIFT_ORDER.indexOf(a as (typeof SHIFT_ORDER)[number]);
    const ib = SHIFT_ORDER.indexOf(b as (typeof SHIFT_ORDER)[number]);
    if (ia === -1 && ib === -1) return a.localeCompare(b);
    if (ia === -1) return 1;
    if (ib === -1) return -1;
    return ia - ib;
  });

  return {
    assignments,
    staff,
    days: Array.from({ length: maxDay + 1 }, (_, i) => i),
    shifts: shiftsSeen,
    rowCount,
    previewCount,
    truncated: previewCount < rowCount,
    empty: false,
  };
}

/** `staff|day` -> assignment, for O(1) cell lookup while rendering. */
export function assignmentIndex(schedule: ResolvedSchedule): Map<string, Assignment> {
  const index = new Map<string, Assignment>();
  for (const assignment of schedule.assignments) {
    // A staff member has at most one shift per day (the `one_shift`
    // constraint), so a collision means malformed rows; first wins and the
    // grid stays drawable.
    const key = `${assignment.staff}|${assignment.day}`;
    if (!index.has(key)) index.set(key, assignment);
  }
  return index;
}

/* ================================================================== *
 * The decorative layer
 * ================================================================== */

export type CellHeat = "seed" | "hot" | "cool";

/**
 * Which cells are lit at pulse `p`.
 *
 * Decorative — there is no per-cell or candidate-schedule data in ANY progress
 * message, so this cannot be otherwise. What is real is that the function is
 * only ever called with a new `pulse` when the solver reports a new incumbent:
 * the cadence is the data, the positions are not.
 *
 * Pure and deterministic on purpose. No timer, no `Math.random`: two tabs
 * watching the same run draw the same frame, a re-render draws the same frame,
 * and a test can assert on it.
 */
export function decorativeCells(
  pulse: number,
  rows: number,
  cols: number,
): Map<number, CellHeat> {
  const cells = new Map<number, CellHeat>();
  if (rows <= 0 || cols <= 0) return cells;

  for (let index = 0; index < rows * cols; index += 1) {
    const row = Math.floor(index / cols);
    const col = index % cols;
    // A stable "already assigned" wash that does not move between pulses, so
    // the eye reads the flicker as change against a background rather than as
    // total noise.
    if (hash(index * 7 + 1, 0) % 100 < 34) cells.set(index, "seed");

    // The moving layer. Two intensities so a pulse reads as a redistribution
    // of effort rather than a strobe.
    const h = hash(index, pulse + 1);
    if (h % 1000 < 90) cells.set(index, "hot");
    else if (h % 1000 < 210) cells.set(index, "cool");
    // Later days depend on earlier ones through the rest constraint, so
    // biasing activity rightwards over time at least gestures at the shape of
    // the search. Still decorative.
    else if ((h >> 3) % 1000 < 60 && col >= row) cells.set(index, "cool");
  }
  return cells;
}

/** A 32-bit integer hash. Not cryptographic and not meant to be — it needs to
 *  be stable across tabs and cheap enough to run per cell per pulse. */
function hash(a: number, b: number): number {
  let x = (a * 0x9e37_79b1) ^ (b * 0x85eb_ca6b);
  x = Math.imul(x ^ (x >>> 15), 0x2545_f491);
  x ^= x >>> 13;
  return Math.abs(x);
}
