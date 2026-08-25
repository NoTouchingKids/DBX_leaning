/**
 * The instance parse, the result resolution and the Gantt layout maths.
 *
 * These are the parts that can be silently wrong. A bar placed off a
 * mis-derived span still renders; a truncated preview still draws a full-
 * looking schedule; a lane for a machine no row mentioned still lays out. None
 * of it throws, which is exactly why it is tested here rather than left to the
 * render smoke test.
 */

import { describe, expect, it } from "vitest";

import type { LogMessage, ResultMessage } from "@/lib/envelope";
import {
  decorativeBars,
  layoutGantt,
  PLOT_WIDTH,
  resolveInstance,
  resolveSchedule,
  STAGES,
  type ResolvedSchedule,
} from "./schedule";

function log(seq: number, message: string, phase = "input"): LogMessage {
  return {
    type: "log",
    run_id: "run-000000000001",
    seq,
    ts: seq * 1000,
    message,
    level: "INFO",
    source: "model",
    phase,
    client_visible: true,
  };
}

/** The real `build()` lines, copied from `job/models/ortools_jobshop/model.py`. */
const SHAPE_LOG = log(
  2,
  "60 jobs / 247 operations over 5 machines (mix, rest, bake, decorate, pack); " +
    "4210 machine-minutes of work, lower bound on makespan 1100 min; " +
    "the jobs stand for 1203 sales transactions",
);
const BUILT_LOG = log(
  3,
  "built: 247 interval variables, 5 no-overlap constraints, horizon 4210 min. " +
    "CP-SAT has no variable or constraint cap; the Gurobi models stop at 2000/2000.",
  "build",
);
const BUILT_WITH_DEADLINE = log(
  3,
  "built: 247 interval variables, 5 no-overlap constraints, horizon 900 min, deadline 900 min. " +
    "CP-SAT has no variable or constraint cap; the Gurobi models stop at 2000/2000.",
  "build",
);

function result(
  preview: Array<Record<string, unknown>>,
  overrides: Partial<ResultMessage> = {},
): ResultMessage {
  return {
    type: "result",
    run_id: "run-000000000001",
    seq: 99,
    ts: 99_000,
    preview,
    row_count: preview.length,
    fetch_hint: {},
    chunk_index: 0,
    final: true,
    ...overrides,
  };
}

/** A row exactly as `JobShopModel.results()` builds it: the operation fields
 *  merged with the run-level ones, repeated per row. */
function row(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    job_id: 0,
    job_label: "Reykjavik Rye x40 @f3001 2026-01-01",
    operation_index: 0,
    machine_id: 0,
    machine_label: "mix",
    start_minute: 0,
    duration_minutes: 20,
    end_minute: 20,
    makespan: 400,
    best_bound: 400,
    solver_status: "OPTIMAL",
    solutions_found: 6,
    wall_time_seconds: 3.2,
    n_jobs: 60,
    n_machines: 5,
    n_operations: 247,
    seed: 20260824,
    data_source: "samples.bakehouse.sales_transactions",
    data_synthetic: false,
    data_rows: 400,
    data_fallback_reason: null,
    ...over,
  };
}

describe("resolveInstance", () => {
  it("reads the shop floor, the bound and the machine names out of the build log", () => {
    const shape = resolveInstance([SHAPE_LOG, BUILT_LOG], null);
    expect(shape.source).toBe("log");
    expect(shape.jobs).toBe(60);
    expect(shape.operations).toBe(247);
    expect(shape.machines).toBe(5);
    expect(shape.machineNames).toEqual(["mix", "rest", "bake", "decorate", "pack"]);
    expect(shape.totalMinutes).toBe(4210);
    expect(shape.makespanLowerBound).toBe(1100);
    expect(shape.transactions).toBe(1203);
    expect(shape.horizon).toBe(4210);
    // No deadline in this build line, which is the ordinary case and the one
    // where INFEASIBLE is unreachable.
    expect(shape.deadlineMinutes).toBeNull();
  });

  it("picks up a deadline, which is the only route to INFEASIBLE", () => {
    expect(resolveInstance([SHAPE_LOG, BUILT_WITH_DEADLINE], null).deadlineMinutes).toBe(900);
  });

  it("falls back to the model's own stage list when no log has arrived", () => {
    const shape = resolveInstance([], null);
    expect(shape.source).toBe("default");
    expect(shape.machines).toBe(STAGES.length);
    expect(shape.machineNames).toEqual([...STAGES]);
    expect(shape.makespanLowerBound).toBeNull();
  });

  it("uses payload counts alone when the log is missing, and says so", () => {
    const shape = resolveInstance([], { nJobs: 24, nMachines: 5, nOperations: 96 });
    expect(shape.source).toBe("payload");
    expect(shape.jobs).toBe(24);
    expect(shape.operations).toBe(96);
    // Names are not in any payload, so they stay the model's defaults.
    expect(shape.machineNames).toEqual([...STAGES]);
  });

  it("lets the payload counts win over the prose, which can be reworded", () => {
    const shape = resolveInstance([SHAPE_LOG, BUILT_LOG], {
      nJobs: 12,
      nMachines: 5,
      nOperations: 44,
    });
    expect(shape.jobs).toBe(12);
    expect(shape.operations).toBe(44);
    // Everything the payload does not carry still comes from the log.
    expect(shape.makespanLowerBound).toBe(1100);
  });

  it("takes the most recent build line when a session watched two runs", () => {
    const older = log(1, "10 jobs / 30 operations over 5 machines (mix, rest, bake, decorate, pack)");
    const shape = resolveInstance([older, SHAPE_LOG], null);
    expect(shape.jobs).toBe(60);
  });

  it("never reports zero machines, because zero lanes is not drawable", () => {
    const shape = resolveInstance([], { nJobs: 1, nMachines: 0, nOperations: 1 });
    expect(shape.machines).toBe(STAGES.length);
  });
});

describe("resolveSchedule", () => {
  it("is empty, not zero-row, when no result message has been seen", () => {
    const schedule = resolveSchedule([]);
    expect(schedule.empty).toBe(true);
    expect(schedule.rowCount).toBe(0);
    expect(schedule.truncated).toBe(false);
  });

  it("distinguishes a run that succeeded with zero rows from one with none", () => {
    const schedule = resolveSchedule([result([], { row_count: 0 })]);
    expect(schedule.empty).toBe(false);
    expect(schedule.rowCount).toBe(0);
  });

  it("lifts the run-level columns off the first row that carries them", () => {
    const schedule = resolveSchedule([result([row(), row({ machine_id: 1, start_minute: 20 })])]);
    expect(schedule.makespan).toBe(400);
    expect(schedule.bestBound).toBe(400);
    expect(schedule.solverStatus).toBe("OPTIMAL");
    expect(schedule.solutionsFound).toBe(6);
    expect(schedule.dataSynthetic).toBe(false);
    expect(schedule.machineIds).toEqual([0, 1]);
    expect(schedule.machineLabels.get(0)).toBe("mix");
  });

  it("costs one malformed row that row, not the view", () => {
    const schedule = resolveSchedule([
      result([
        row(),
        row({ machine_id: null }),
        row({ start_minute: -4 }),
        // A zero-length operation is not something a machine does; the model
        // clamps to MIN_OPERATION_MINUTES, so a zero here is malformed.
        row({ duration_minutes: 0, end_minute: 0 }),
        // Another model's preview rows, if two resolvers ever see one snapshot.
        { staff: "staff-00", day: 0, shift: "morning" },
      ]),
    ]);
    expect(schedule.operations).toHaveLength(1);
    expect(schedule.previewCount).toBe(5);
  });

  it("recovers a missing duration from end_minute rather than dropping the row", () => {
    const schedule = resolveSchedule([
      result([row({ duration_minutes: null, start_minute: 30, end_minute: 55 })]),
    ]);
    expect(schedule.operations[0]?.duration).toBe(25);
    expect(schedule.operations[0]?.end).toBe(55);
  });

  it("derives the end from the duration so a bar's width and right edge agree", () => {
    // The DDL carries end_minute and the model always writes start + duration.
    // A row disagreeing with itself must not produce a bar whose drawn width
    // contradicts its drawn end.
    const schedule = resolveSchedule([
      result([row({ start_minute: 10, duration_minutes: 20, end_minute: 999 })]),
    ]);
    expect(schedule.operations[0]?.end).toBe(30);
  });

  it("flags a downsampled preview, summing row_count across chunks", () => {
    const schedule = resolveSchedule([
      result([row(), row({ machine_id: 1 })], { row_count: 1700, final: false }),
      result([row({ machine_id: 2 })], { row_count: 0, chunk_index: 1 }),
    ]);
    expect(schedule.previewCount).toBe(3);
    expect(schedule.rowCount).toBe(1700);
    expect(schedule.truncated).toBe(true);
  });
});

describe("layoutGantt", () => {
  const shape = { machines: 5, machineNames: [...STAGES] };

  it("places bars as a fraction of the span, in the right lane", () => {
    const schedule = resolveSchedule([
      result([
        row({
          machine_id: 0,
          start_minute: 0,
          duration_minutes: 100,
          end_minute: 100,
          makespan: 200,
        }),
        row({
          machine_id: 2,
          machine_label: "bake",
          start_minute: 100,
          duration_minutes: 100,
          end_minute: 200,
          makespan: 200,
        }),
      ]),
    ]);
    const layout = layoutGantt(schedule, shape);
    expect(layout.span).toBe(200);
    expect(layout.lanes).toHaveLength(5);
    expect(layout.lanes[0]?.bars).toHaveLength(1);
    expect(layout.lanes[1]?.bars).toHaveLength(0);
    expect(layout.lanes[0]?.bars[0]?.x).toBe(0);
    expect(layout.lanes[0]?.bars[0]?.width).toBe(PLOT_WIDTH / 2);
    expect(layout.lanes[2]?.bars[0]?.x).toBe(PLOT_WIDTH / 2);
    expect(layout.lanes[2]?.label).toBe("bake");
    expect(layout.barCount).toBe(2);
  });

  it("fits the axis to the recorded makespan when the preview is truncated", () => {
    // Fitting to the largest end minute present would stretch a sampled subset
    // across the full width and make a partial schedule look complete.
    const schedule = resolveSchedule([
      result([row({ start_minute: 0, duration_minutes: 20, end_minute: 20, makespan: 400 })], {
        row_count: 1700,
      }),
    ]);
    const layout = layoutGantt(schedule, shape);
    expect(layout.span).toBe(400);
    expect(layout.spanFromMakespan).toBe(true);
    expect(layout.lanes[0]?.bars[0]?.width).toBe((20 / 400) * PLOT_WIDTH);
  });

  it("does not let a widened hairline bar overrun the makespan", () => {
    // A one-minute operation on a long schedule is sub-pixel and gets widened
    // to stay visible; that must not push the last operation past the axis.
    const schedule = resolveSchedule([
      result([row({ start_minute: 3999, duration_minutes: 1, end_minute: 4000, makespan: 4000 })]),
    ]);
    const bar = layoutGantt(schedule, shape).lanes[0]?.bars[0];
    expect(bar).toBeDefined();
    expect(bar!.width).toBeGreaterThan(0.25);
    expect(bar!.x + bar!.width).toBeLessThanOrEqual(PLOT_WIDTH);
    expect(bar!.x).toBeGreaterThanOrEqual(0);
  });

  it("never divides by a zero span", () => {
    const empty: ResolvedSchedule = resolveSchedule([]);
    const layout = layoutGantt(empty, shape);
    expect(layout.span).toBe(1);
    expect(layout.barCount).toBe(0);
    expect(layout.lanes).toHaveLength(5);
    expect(layout.lanes.every((lane) => lane.bars.length === 0)).toBe(true);
  });

  it("adds a lane for a machine id the declared shape did not mention", () => {
    // A row must not be dropped on the floor because the log said five
    // machines and the rows used a sixth.
    const schedule = resolveSchedule([result([row({ machine_id: 7, machine_label: "wrap" })])]);
    const layout = layoutGantt(schedule, { machines: 5, machineNames: [...STAGES] });
    expect(layout.lanes).toHaveLength(8);
    expect(layout.lanes[7]?.label).toBe("wrap");
    expect(layout.lanes[7]?.bars).toHaveLength(1);
    // Lanes the instance declared but no row used keep their names.
    expect(layout.lanes[4]?.label).toBe("pack");
    // And ones invented purely to reach the id are still identifiable.
    expect(layout.lanes[6]?.label).toBe("machine-6");
  });

  it("accumulates busy minutes per machine, which is what utilisation is", () => {
    const schedule = resolveSchedule([
      result([
        row({ start_minute: 0, duration_minutes: 30, end_minute: 30, makespan: 100 }),
        row({ start_minute: 50, duration_minutes: 20, end_minute: 70, makespan: 100 }),
      ]),
    ]);
    const layout = layoutGantt(schedule, shape);
    expect(layout.lanes[0]?.busyMinutes).toBe(50);
    expect(layout.span).toBe(100);
  });
});

describe("decorativeBars", () => {
  it("never overlaps two bars in one lane — the one thing the model forbids", () => {
    for (let pulse = 0; pulse < 12; pulse += 1) {
      for (const bars of laneGroups(decorativeBars(pulse, 5))) {
        const sorted = [...bars].sort((a, b) => a.x - b.x);
        for (let i = 1; i < sorted.length; i += 1) {
          const previous = sorted[i - 1]!;
          expect(sorted[i]!.x).toBeGreaterThanOrEqual(previous.x + previous.width);
        }
      }
    }
  });

  it("stays inside the plot", () => {
    for (const bar of decorativeBars(3, 5)) {
      expect(bar.x).toBeGreaterThanOrEqual(0);
      expect(bar.x + bar.width).toBeLessThanOrEqual(PLOT_WIDTH);
      expect(bar.width).toBeGreaterThan(0);
    }
  });

  it("is deterministic, so two tabs and a re-render draw the same frame", () => {
    expect(decorativeBars(4, 5)).toEqual(decorativeBars(4, 5));
  });

  it("redraws on a new pulse — the cadence IS the information here", () => {
    expect(decorativeBars(4, 5)).not.toEqual(decorativeBars(5, 5));
  });

  it("returns nothing for zero lanes rather than looping", () => {
    expect(decorativeBars(1, 0)).toEqual([]);
    expect(decorativeBars(1, -3)).toEqual([]);
  });
});

function laneGroups<T extends { lane: number }>(bars: readonly T[]): T[][] {
  const groups = new Map<number, T[]>();
  for (const bar of bars) {
    const group = groups.get(bar.lane);
    if (group) group.push(bar);
    else groups.set(bar.lane, [bar]);
  }
  return [...groups.values()];
}
