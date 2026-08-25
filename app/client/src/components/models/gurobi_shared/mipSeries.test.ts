/**
 * The arithmetic behind both Gurobi views.
 *
 * What can actually be wrong here is numeric and ordering-related: the
 * ±1e100-turned-null sentinel plotted as a zero, a duplicated backfill message
 * counted as a second incumbent, a gap left as a fraction on an axis labelled
 * "%", a zero node count fed to a log axis, and an empty progress list read as
 * "loading" instead of as a real state. All of those are here.
 */

import { describe, expect, it } from "vitest";

import type { ProgressMessage, StatusMessage } from "@/lib/envelope";

import {
  deriveMipSeries,
  finite,
  hasPlottable,
  incumbentActivity,
  terminalDetail,
} from "./mipSeries";

/** Shaped exactly like `job/drivers/gurobi.py::_sample_progress` emits one. */
function progress(
  seq: number,
  gap: number | null,
  payload: Record<string, unknown>,
  overrides: Partial<ProgressMessage> = {},
): ProgressMessage {
  return {
    type: "progress",
    run_id: "run-00000000abcd",
    seq,
    ts: 1_700_000_000_000 + seq,
    elapsed_seconds: seq * 2,
    percent_complete: null,
    primary_metric: gap,
    primary_metric_label: "mip_gap",
    payload,
    ...overrides,
  };
}

function sample(
  seq: number,
  {
    gap = null,
    incumbent = null,
    bound = null,
    nodes = 0,
    remaining = 0,
    solutions = 0,
  }: {
    gap?: number | null;
    incumbent?: number | null;
    bound?: number | null;
    nodes?: number;
    remaining?: number;
    solutions?: number;
  },
): ProgressMessage {
  return progress(seq, gap, {
    best_bound: bound,
    incumbent,
    nodes_explored: nodes,
    nodes_remaining: remaining,
    solution_count: solutions,
  });
}

function status(seq: number, s: StatusMessage["status"], detail: string | null): StatusMessage {
  return { type: "status", run_id: "run-00000000abcd", seq, ts: seq, status: s, detail };
}

describe("finite", () => {
  it("rejects the nulls the driver emits for the pre-incumbent sentinel", () => {
    // `_real_or_none` maps Gurobi's ±1e100 to None. `lib/models.ts` declares
    // these fields `number`; the wire says otherwise, and a chart that
    // believes the declaration plots the null as 0.
    expect(finite(null)).toBeNull();
    expect(finite(undefined)).toBeNull();
    expect(finite(Number.NaN)).toBeNull();
    expect(finite(Number.POSITIVE_INFINITY)).toBeNull();
  });

  it("rejects non-numbers rather than coercing them", () => {
    expect(finite("42")).toBeNull();
    // Number(true) is 1, which would invent a data point.
    expect(finite(true)).toBeNull();
  });

  it("keeps zero, which is a real node count and a real objective", () => {
    expect(finite(0)).toBe(0);
    expect(finite(-3.5)).toBe(-3.5);
  });
});

describe("deriveMipSeries", () => {
  it("orders by seq, not by arrival — the store appends in arrival order", () => {
    const points = deriveMipSeries([
      sample(9, { nodes: 300 }),
      sample(3, { nodes: 10 }),
      sample(6, { nodes: 90 }),
    ]);
    expect(points.map((p) => p.seq)).toEqual([3, 6, 9]);
    expect(points.map((p) => p.nodesExplored)).toEqual([10, 90, 300]);
  });

  it("drops a duplicated message instead of double-counting it", () => {
    // A message can arrive both from IndexedDB hydration and from the live
    // stream. Duplicated, it would read as a second incumbent event.
    const dup = sample(4, { solutions: 1 });
    const points = deriveMipSeries([dup, dup, sample(5, { solutions: 1 })]);
    expect(points).toHaveLength(2);
    expect(points.filter((p) => p.newIncumbent)).toHaveLength(1);
  });

  it("carries the pre-feasible stretch through as nulls", () => {
    const [point] = deriveMipSeries([sample(1, { incumbent: null, bound: null, gap: null })]);
    expect(point?.incumbent).toBeNull();
    expect(point?.bestBound).toBeNull();
    expect(point?.gapPercent).toBeNull();
  });

  it("converts the gap fraction to a percentage", () => {
    // `primary_metric` is |inc - bnd| / |inc|, not a percentage.
    const [point] = deriveMipSeries([sample(1, { gap: 0.0425 })]);
    expect(point?.gapPercent).toBeCloseTo(4.25, 10);
  });

  it("does not assume the gap only shrinks", () => {
    // The bound moves too, so a gap can widen between samples. Nothing in the
    // derivation clamps it to a running minimum.
    const points = deriveMipSeries([sample(1, { gap: 0.1 }), sample(2, { gap: 0.2 })]);
    expect(points[0]?.gapPercent).toBeCloseTo(10, 10);
    expect(points[1]?.gapPercent).toBeCloseTo(20, 10);
  });

  it("nulls a node count a log axis cannot draw, and keeps the raw one", () => {
    const points = deriveMipSeries([sample(1, { nodes: 0 }), sample(2, { nodes: 7 })]);
    expect(points.map((p) => p.nodesExplored)).toEqual([0, 7]);
    expect(points.map((p) => p.nodesLog)).toEqual([null, 7]);
  });

  it("returns an empty series for an empty progress list", () => {
    expect(deriveMipSeries([])).toEqual([]);
  });
});

describe("incumbent pulse detection", () => {
  it("pulses once per solution_count increase, not once per sample", () => {
    const points = deriveMipSeries([
      sample(1, { solutions: 0 }),
      sample(2, { solutions: 1 }),
      sample(3, { solutions: 1 }),
      sample(4, { solutions: 1 }),
      sample(5, { solutions: 2 }),
    ]);
    expect(points.map((p) => p.newIncumbent)).toEqual([false, true, false, false, true]);
    expect(incumbentActivity(points).pulses).toBe(2);
  });

  it("counts a jump of several solutions as ONE observed event", () => {
    // Samples are ~2s apart; two incumbents found inside one window arrive as
    // a single jump, and one event is all the data supports claiming.
    const points = deriveMipSeries([sample(1, { solutions: 0 }), sample(2, { solutions: 4 })]);
    const activity = incumbentActivity(points);
    expect(activity.pulses).toBe(1);
    expect(activity.solutionCount).toBe(4);
  });

  it("pulses on the first sample when it already reports solutions", () => {
    // Attaching to a run in flight: the baseline is zero, so the first
    // non-zero count is a visible event rather than a silent starting value.
    const points = deriveMipSeries([sample(10, { solutions: 3 })]);
    expect(incumbentActivity(points).pulses).toBe(1);
  });

  it("does not pulse on a stale count that arrives late", () => {
    const points = deriveMipSeries([
      sample(1, { solutions: 2 }),
      // Impossible from one solver, but the store is fed by two paths.
      sample(2, { solutions: 1 }),
      sample(3, { solutions: 2 }),
    ]);
    expect(incumbentActivity(points).pulses).toBe(1);
  });

  it("reports the last pulse's seq so a redraw is keyed to the event", () => {
    const points = deriveMipSeries([
      sample(4, { solutions: 1 }),
      sample(5, { solutions: 1 }),
      sample(6, { solutions: 2 }),
      sample(7, { solutions: 2 }),
    ]);
    expect(incumbentActivity(points).lastPulseSeq).toBe(6);
  });

  it("reports no activity at all for an empty run", () => {
    expect(incumbentActivity([])).toEqual({
      pulses: 0,
      solutionCount: null,
      lastPulseSeq: null,
      latest: null,
    });
  });

  it("keeps a null solution_count null rather than calling it zero", () => {
    const points = deriveMipSeries([progress(1, null, {})]);
    expect(incumbentActivity(points).solutionCount).toBeNull();
    expect(points[0]?.newIncumbent).toBe(false);
  });
});

describe("hasPlottable", () => {
  it("is false for a run with samples whose every value is null", () => {
    // A MIP can report progress before it has a bound, an incumbent or a
    // single explored node. Fitting an axis to that draws a broken frame.
    const points = deriveMipSeries([
      progress(1, null, { incumbent: null, best_bound: null }),
      progress(2, null, { incumbent: null, best_bound: null }),
    ]);
    expect(hasPlottable(points, ["incumbent", "bestBound", "gapPercent"])).toBe(false);
  });

  it("is false for no progress at all", () => {
    expect(hasPlottable([], ["incumbent"])).toBe(false);
  });

  it("is true as soon as one field on one sample is numeric", () => {
    const points = deriveMipSeries([sample(1, { bound: 41_220 })]);
    expect(hasPlottable(points, ["incumbent", "bestBound", "gapPercent"])).toBe(true);
  });
});

describe("terminalDetail", () => {
  it("takes the highest-seq status, not the last appended", () => {
    expect(
      terminalDetail([status(90, "SUCCEEDED", "time limit reached"), status(2, "RUNNING", null)]),
    ).toBe("time limit reached");
  });

  it("is null while the run is still going", () => {
    expect(terminalDetail([status(1, "QUEUED", null), status(2, "RUNNING", null)])).toBeNull();
  });

  it("is null when nothing has been seen", () => {
    expect(terminalDetail([])).toBeNull();
  });
});
