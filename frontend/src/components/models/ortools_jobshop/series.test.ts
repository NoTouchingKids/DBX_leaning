/**
 * The derivation, over the shapes this stream really takes.
 *
 * The cases worth a test are the ones that do not throw when they are wrong:
 * a null incumbent plotted as zero, an ABSENT `conflicts` read as zero rather
 * than as a hole, an improvement counted twice because backfill re-delivered
 * a sample, a `percent_complete` surfaced without its basis.
 */

import { describe, expect, it } from "vitest";

import type { ProgressMessage, StatusMessage } from "@/lib/envelope";
import {
  deriveJobshopSeries,
  finite,
  hasPlottable,
  solveActivity,
  solverClock,
  terminalDetail,
} from "./series";

function progress(
  seq: number,
  payload: Record<string, unknown>,
  overrides: Partial<ProgressMessage> = {},
): ProgressMessage {
  return {
    type: "progress",
    run_id: "run-000000000001",
    seq,
    ts: seq * 1000,
    elapsed_seconds: seq,
    percent_complete: null,
    primary_metric: null,
    primary_metric_label: "relative_gap",
    payload,
    ...overrides,
  };
}

const BASIS = "elapsed_solver_time_against_time_limit";

describe("finite", () => {
  it("rejects everything a chart cannot plot, booleans included", () => {
    expect(finite(4)).toBe(4);
    expect(finite(0)).toBe(0);
    expect(finite(null)).toBeNull();
    expect(finite(undefined)).toBeNull();
    // `Number(true)` is 1, which would invent a data point out of `final: true`.
    expect(finite(true)).toBeNull();
    expect(finite(Number.POSITIVE_INFINITY)).toBeNull();
    expect(finite("12")).toBeNull();
  });
});

describe("deriveJobshopSeries", () => {
  it("keeps a pre-feasible sample's incumbent and bound null rather than zero", () => {
    // CP-SAT's pre-search bound is a real infinity, nulled by `_finite` before
    // it leaves the model. A chart reading these as 0 would drag the makespan
    // axis to the origin for the whole pre-feasible stretch.
    const [point] = deriveJobshopSeries([
      progress(1, { incumbent: null, best_bound: null, solutions_found: 0, final: false }),
    ]);
    expect(point?.incumbent).toBeNull();
    expect(point?.bestBound).toBeNull();
    expect(point?.gapPercent).toBeNull();
    expect(point?.solutionsFound).toBe(0);
  });

  it("treats an ABSENT conflicts/branches as null, not as zero", () => {
    // The final sample is emitted after `solve()` returns with no callback in
    // hand, so `_emit_progress` writes neither key at all. Absent and null must
    // land in the same place: a hole in the line.
    const [withCounters, withoutCounters] = deriveJobshopSeries([
      progress(1, { solutions_found: 1, conflicts: 40, branches: 90, final: false }),
      progress(2, { solutions_found: 3, final: true, solver_status: "OPTIMAL" }),
    ]);
    expect(withCounters?.conflicts).toBe(40);
    expect(withCounters?.branches).toBe(90);
    expect(withoutCounters?.conflicts).toBeNull();
    expect(withoutCounters?.branches).toBeNull();
    expect(withoutCounters?.final).toBe(true);
    expect(withoutCounters?.solverStatus).toBe("OPTIMAL");
    // Only the final sample carries a solver status; the earlier one must not
    // borrow it.
    expect(withCounters?.solverStatus).toBeNull();
  });

  it("nulls a zero conflict count for the log axis rather than nudging it to 1", () => {
    const points = deriveJobshopSeries([
      progress(1, { conflicts: 0, solutions_found: 1 }),
      progress(2, { conflicts: 1, solutions_found: 2 }),
    ]);
    expect(points[0]?.conflicts).toBe(0);
    expect(points[0]?.conflictsLog).toBeNull();
    expect(points[1]?.conflictsLog).toBe(1);
  });

  it("orders by seq and dedupes, so a re-delivered sample is not a second improvement", () => {
    const first = progress(10, { solutions_found: 1 });
    const second = progress(11, { solutions_found: 2 });
    // Arrival order: live, then a hydrate that re-delivers both out of order.
    const points = deriveJobshopSeries([second, first, second, first]);
    expect(points.map((p) => p.seq)).toEqual([10, 11]);
    expect(solveActivity(points).improvements).toBe(2);
  });

  it("scales the gap to a percentage and reads it off primary_metric", () => {
    const [point] = deriveJobshopSeries([
      progress(1, { incumbent: 100, best_bound: 96 }, { primary_metric: 0.04 }),
    ]);
    expect(point?.gapPercent).toBeCloseTo(4);
  });

  it("falls back to payload wall_time when elapsed_seconds is unusable", () => {
    const [point] = deriveJobshopSeries([
      progress(1, { wall_time: 7.5 }, { elapsed_seconds: Number.NaN }),
    ]);
    expect(point?.elapsed).toBe(7.5);
  });

  it("carries the instance counts through, so the floor can be sized without a log", () => {
    const [point] = deriveJobshopSeries([
      progress(1, { n_jobs: 60, n_machines: 5, n_operations: 247 }),
    ]);
    expect(point?.nJobs).toBe(60);
    expect(point?.nMachines).toBe(5);
    expect(point?.nOperations).toBe(247);
  });
});

describe("solveActivity", () => {
  it("counts observed events, not the raw delta in solutions_found", () => {
    // The model always reports its first solution then throttles, so a
    // portfolio search that improved four times inside one window arrives as
    // ONE observed increment. Pacing on the delta would fire four frames for
    // one message.
    const points = deriveJobshopSeries([
      progress(1, { solutions_found: 1 }),
      progress(2, { solutions_found: 5 }),
      progress(3, { solutions_found: 5 }),
    ]);
    const activity = solveActivity(points);
    expect(activity.improvements).toBe(2);
    expect(activity.solutionsFound).toBe(5);
    expect(activity.lastImprovementSeq).toBe(2);
  });

  it("is empty and does not throw on a run with no progress at all", () => {
    const activity = solveActivity([]);
    expect(activity).toEqual({
      improvements: 0,
      solutionsFound: null,
      lastImprovementSeq: null,
      latest: null,
      solverStatus: null,
    });
  });

  it("keeps a solver status reported mid-stream even if later samples lack one", () => {
    // Nothing guarantees the final sample is the last thing the store holds
    // after a hydrate/live overlap, so the status must not be read off the
    // tail alone.
    const points = deriveJobshopSeries([
      progress(1, { solutions_found: 1 }),
      progress(2, { solutions_found: 2, final: true, solver_status: "FEASIBLE" }),
    ]);
    expect(solveActivity(points).solverStatus).toBe("FEASIBLE");
  });
});

describe("solverClock", () => {
  it("reports the latest percentage together with the model's own basis", () => {
    const points = deriveJobshopSeries([
      progress(1, { percent_complete_basis: BASIS }, { percent_complete: 12.5 }),
      progress(2, { percent_complete_basis: BASIS }, { percent_complete: 64 }),
    ]);
    expect(solverClock(points)).toEqual({ percent: 64, basis: BASIS });
  });

  it("is null with no time limit configured — there is no honest denominator", () => {
    const points = deriveJobshopSeries([
      progress(1, { percent_complete_basis: BASIS }, { percent_complete: null }),
    ]);
    expect(solverClock(points).percent).toBeNull();
    expect(solverClock(points).basis).toBe(BASIS);
  });
});

describe("hasPlottable", () => {
  it("is false for an INFEASIBLE run's single all-null sample", () => {
    // The one sample such a run emits has a null incumbent, a null bound and a
    // null gap; an axis fitted to nothing but nulls renders as a broken frame.
    const points = deriveJobshopSeries([
      progress(1, { incumbent: null, best_bound: null, final: true, solver_status: "INFEASIBLE" }),
    ]);
    expect(points).toHaveLength(1);
    expect(hasPlottable(points, ["incumbent", "bestBound", "gapPercent"])).toBe(false);
  });

  it("is true as soon as one field on one sample is real", () => {
    const points = deriveJobshopSeries([
      progress(1, { incumbent: null, best_bound: null }),
      progress(2, { incumbent: 410, best_bound: null }),
    ]);
    expect(hasPlottable(points, ["incumbent", "bestBound", "gapPercent"])).toBe(true);
  });
});

describe("terminalDetail", () => {
  const status = (seq: number, over: Partial<StatusMessage> = {}): StatusMessage => ({
    type: "status",
    run_id: "run-000000000001",
    seq,
    ts: seq * 1000,
    status: "SUCCEEDED",
    detail: null,
    ...over,
  });

  it("takes the highest seq, not the last appended", () => {
    expect(
      terminalDetail([
        status(9, { detail: "optimal: makespan 412 min" }),
        status(2, { status: "RUNNING", detail: "stale" }),
      ]),
    ).toBe("optimal: makespan 412 min");
  });

  it("is null while the run is still going", () => {
    expect(terminalDetail([status(1, { status: "RUNNING", detail: "solving" })])).toBeNull();
    expect(terminalDetail([])).toBeNull();
  });
});
