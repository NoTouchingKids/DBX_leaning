/**
 * The grid's inputs: what it reads out of the log stream, what it reads out of
 * the result rows, and the decorative layer's determinism.
 *
 * The failure modes worth a test: a grid drawn at the wrong dimensions with
 * total confidence, a downsampled preview presented as the whole schedule, one
 * malformed row taking the view down, and a decorative frame that differs
 * between two tabs watching the same run.
 */

import { describe, expect, it } from "vitest";

import type { LogMessage, ResultMessage } from "@/lib/envelope";

import {
  assignmentIndex,
  decorativeCells,
  DEFAULT_SHAPE,
  parseCoverage,
  parseScheduleShape,
  resolveSchedule,
} from "./schedule";

function log(seq: number, message: string, phase = "input"): LogMessage {
  return {
    type: "log",
    run_id: "run-00000000abcd",
    seq,
    ts: 1_700_000_000_000 + seq,
    message,
    level: "INFO",
    source: "model",
    phase,
    client_visible: true,
  };
}

function result(
  preview: Array<Record<string, unknown>>,
  rowCount = preview.length,
): ResultMessage {
  return {
    type: "result",
    run_id: "run-00000000abcd",
    seq: 900,
    ts: 1_700_000_000_900,
    preview,
    row_count: rowCount,
    fetch_hint: { table: "main.default.results_gurobi_scheduling", key: "run_id" },
    chunk_index: 0,
    final: true,
  };
}

function shift(staff: string, day: number, name: string, preferred = false) {
  return { staff, day, shift: name, cost: 1.4, preferred, demand: 3 };
}

describe("parseScheduleShape", () => {
  it("reads the instance out of the model's own build log", () => {
    // `SchedulingModel.build` emits this before the solve, so the grid is the
    // run's real dimensions from the first second of RUNNING.
    expect(parseScheduleShape([log(3, "building: 20 staff x 14 days x 3 shifts")])).toEqual({
      staffCount: 20,
      days: 14,
      shifts: 3,
      source: "log",
    });
  });

  it("falls back to a plainly labelled default when the line is absent", () => {
    // QUEUED and STARTING have no logs at all. The fallback is marked as such
    // so the UI can say the grid is illustrative rather than implying it is
    // this run's instance.
    expect(parseScheduleShape([])).toEqual(DEFAULT_SHAPE);
    expect(parseScheduleShape([log(1, "reading demand curve")]).source).toBe("default");
  });

  it("takes the most recent build line", () => {
    const shape = parseScheduleShape([
      log(1, "building: 20 staff x 14 days x 3 shifts"),
      log(2, "building: 8 staff x 7 days x 3 shifts"),
    ]);
    expect(shape.staffCount).toBe(8);
    expect(shape.days).toBe(7);
  });

  it("rejects a nonsense instance rather than drawing a zero-row grid", () => {
    expect(parseScheduleShape([log(1, "building: 0 staff x 14 days x 3 shifts")])).toEqual(
      DEFAULT_SHAPE,
    );
  });
});

describe("parseCoverage", () => {
  it("reads the demand total and its provenance", () => {
    const coverage = parseCoverage([
      log(2, "coverage: 236 staff-shifts over 14 days, from hourly_trip_demand"),
    ]);
    expect(coverage).toEqual({
      totalDemand: 236,
      days: 14,
      derivedFrom: "hourly_trip_demand",
      clipped: false,
    });
  });

  it("notices the clipped-to-capacity warning", () => {
    // The model logs this at WARNING, and it is the most useful thing to show
    // beside an INFEASIBLE: the workforce was already the binding constraint.
    const coverage = parseCoverage([
      log(2, "coverage: 900 staff-shifts over 14 days, from synthetic_demand, clipped to workforce capacity"),
    ]);
    expect(coverage?.clipped).toBe(true);
    expect(coverage?.derivedFrom).toBe("synthetic_demand");
  });

  it("is null when the line has not arrived", () => {
    expect(parseCoverage([])).toBeNull();
  });
});

describe("resolveSchedule", () => {
  it("distinguishes 'no result message' from 'a result with zero rows'", () => {
    expect(resolveSchedule([]).empty).toBe(true);
    const zero = resolveSchedule([result([], 0)]);
    expect(zero.empty).toBe(false);
    expect(zero.rowCount).toBe(0);
  });

  it("builds the staff, day and shift axes from the rows themselves", () => {
    const schedule = resolveSchedule([
      result([
        shift("staff-01", 2, "night"),
        shift("staff-00", 0, "morning"),
        shift("staff-01", 1, "evening"),
      ]),
    ]);
    expect(schedule.staff).toEqual(["staff-00", "staff-01"]);
    // Days are contiguous: an unassigned day is still a column.
    expect(schedule.days).toEqual([0, 1, 2]);
    // Canonical SHIFTS order, not alphabetical.
    expect(schedule.shifts).toEqual(["morning", "evening", "night"]);
  });

  it("sorts an unrecognised shift after the canonical three instead of dropping it", () => {
    const schedule = resolveSchedule([
      result([shift("staff-00", 0, "twilight"), shift("staff-00", 1, "night")]),
    ]);
    expect(schedule.shifts).toEqual(["night", "twilight"]);
  });

  it("skips a malformed row and keeps the rest", () => {
    // `preview` is Array<Record<string, unknown>> and nothing server-side
    // looks inside it. One bad row costs that row, not the view.
    const schedule = resolveSchedule([
      result([
        shift("staff-00", 0, "morning"),
        { staff: "staff-01", day: "tuesday", shift: "night" },
        { day: 1, shift: "night" },
        shift("staff-02", 1, "night"),
      ]),
    ]);
    expect(schedule.assignments).toHaveLength(2);
    expect(schedule.previewCount).toBe(4);
  });

  it("flags a downsampled preview so a partial grid is not read as the schedule", () => {
    // `job/emitter.py` downsamples the preview; `row_count` is the durable
    // truth. A grid missing rows with no note is a lie.
    const schedule = resolveSchedule([result([shift("staff-00", 0, "morning")], 640)]);
    expect(schedule.truncated).toBe(true);
    expect(schedule.rowCount).toBe(640);
    expect(schedule.previewCount).toBe(1);
  });

  it("sums row counts across chunks", () => {
    const schedule = resolveSchedule([
      result([shift("staff-00", 0, "morning")]),
      result([shift("staff-01", 0, "night")]),
    ]);
    expect(schedule.rowCount).toBe(2);
    expect(schedule.truncated).toBe(false);
  });
});

describe("assignmentIndex", () => {
  it("keys one assignment per staff-day, as the one_shift constraint promises", () => {
    const schedule = resolveSchedule([
      result([shift("staff-00", 0, "morning"), shift("staff-00", 3, "night")]),
    ]);
    const index = assignmentIndex(schedule);
    expect(index.get("staff-00|0")?.shift).toBe("morning");
    expect(index.get("staff-00|3")?.shift).toBe("night");
    expect(index.get("staff-00|1")).toBeUndefined();
  });
});

describe("decorativeCells", () => {
  it("is deterministic, so two tabs on one run draw the same frame", () => {
    expect([...decorativeCells(4, 8, 14)]).toEqual([...decorativeCells(4, 8, 14)]);
  });

  it("changes between pulses — the flicker IS the pulse", () => {
    const before = JSON.stringify([...decorativeCells(4, 8, 14)]);
    const after = JSON.stringify([...decorativeCells(5, 8, 14)]);
    expect(after).not.toBe(before);
  });

  it("never addresses a cell outside the grid", () => {
    const cells = decorativeCells(3, 5, 9);
    for (const index of cells.keys()) {
      expect(index).toBeGreaterThanOrEqual(0);
      expect(index).toBeLessThan(45);
    }
  });

  it("lights something, but nothing like everything", () => {
    // A frame with no change reads as a hung page; a frame that lights the
    // whole grid reads as a strobe.
    const cells = decorativeCells(2, 8, 14);
    expect(cells.size).toBeGreaterThan(0);
    expect(cells.size).toBeLessThan(8 * 14);
  });

  it("returns nothing for a degenerate grid rather than throwing", () => {
    expect(decorativeCells(1, 0, 14).size).toBe(0);
    expect(decorativeCells(1, 8, 0).size).toBe(0);
  });
});
