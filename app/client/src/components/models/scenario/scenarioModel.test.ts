import { describe, expect, it } from "vitest";

import type { ProgressMessage, ResultMessage } from "@/lib/envelope";

import {
  deriveSweep,
  EMPTY_SWEEP,
  locateCell,
  objectivePoints,
  SCAN_CELLS,
  SCAN_COLS,
} from "./scenarioModel";

function progress(over: Partial<ProgressMessage> = {}): ProgressMessage {
  return {
    type: "progress",
    run_id: "run-a",
    seq: 1,
    ts: 1_700_000_000_000,
    elapsed_seconds: 1,
    percent_complete: null,
    primary_metric: null,
    primary_metric_label: "best_objective",
    payload: {},
    ...over,
  };
}

function sweepMessage(
  demand: number,
  capacity: number,
  objective: number,
  over: Partial<ProgressMessage> = {},
): ProgressMessage {
  return progress({
    primary_metric: objective,
    payload: {
      scenarios_done: 10,
      scenarios_total: 72,
      last_scenario: { capacity, demand, unit_cost: 1.0 },
      last_outcome: { served: 800, shortfall: 0, idle: 12, objective },
    },
    ...over,
  });
}

function result(preview: Array<Record<string, unknown>>, over: Partial<ResultMessage> = {}): ResultMessage {
  return {
    type: "result",
    run_id: "run-a",
    seq: 99,
    ts: 1_700_000_000_000,
    preview,
    row_count: preview.length,
    fetch_hint: {},
    chunk_index: 0,
    final: true,
    ...over,
  };
}

describe("locateCell", () => {
  it("matches the model's own iteration order — capacity is the row, demand the column", () => {
    // `scenarios()` products over sorted(grid) = capacity, demand, unit_cost,
    // so the first row of the raster is capacity 0.9 across all six demands.
    expect(locateCell({ last_scenario: { capacity: 0.9, demand: 0.8, unit_cost: 0.9 } })).toEqual({
      index: 0,
      exact: true,
    });
    expect(locateCell({ last_scenario: { capacity: 0.9, demand: 2.1, unit_cost: 1.1 } })).toEqual({
      index: SCAN_COLS - 1,
      exact: true,
    });
    expect(locateCell({ last_scenario: { capacity: 1.0, demand: 0.8, unit_cost: 0.9 } })).toEqual({
      index: SCAN_COLS,
      exact: true,
    });
    expect(locateCell({ last_scenario: { capacity: 1.2, demand: 2.1, unit_cost: 1.1 } })).toEqual({
      index: SCAN_CELLS - 1,
      exact: true,
    });
  });

  it("falls back to a proportional placement for a grid it cannot match", () => {
    // A custom grid: the multipliers are real, they are simply not on the
    // axes this view draws. Half done must still land mid-grid.
    const located = locateCell({
      last_scenario: { capacity: 3.3, demand: 7.7, unit_cost: 1.0 },
      scenarios_done: 36,
      scenarios_total: 72,
    });
    expect(located).toEqual({ index: 11, exact: false });
  });

  it("clamps the proportional placement to the grid", () => {
    expect(locateCell({ scenarios_done: 72, scenarios_total: 72 })?.index).toBe(SCAN_CELLS - 1);
    expect(locateCell({ scenarios_done: 1, scenarios_total: 72 })?.index).toBe(0);
  });

  it("returns null when the message says nothing about position", () => {
    expect(locateCell({})).toBeNull();
    expect(locateCell({ last_scenario: null, scenarios_total: 0 })).toBeNull();
  });
});

describe("deriveSweep", () => {
  it("is empty for an empty stream", () => {
    expect(deriveSweep([])).toEqual(EMPTY_SWEEP);
  });

  it("tracks the best cell only when best_objective actually improves", () => {
    const view = deriveSweep([
      sweepMessage(0.8, 0.9, 100),
      sweepMessage(1.2, 0.9, 180), // improvement -> best moves here (index 2)
      sweepMessage(1.5, 0.9, 180), // same best, no improvement -> best stays
      sweepMessage(2.1, 0.9, 180),
    ]);
    expect(view.bestObjective).toBe(180);
    expect(view.bestCell).toBe(2);
    expect(view.head).toBe(5);
  });

  it("does not let a non-monotonic metric drag the best backwards", () => {
    // primary_metric is a running max model-side, so a lower value here means
    // a reordered or replayed message, not a worse incumbent.
    const view = deriveSweep([sweepMessage(1.2, 0.9, 180), sweepMessage(1.5, 0.9, 50)]);
    expect(view.bestObjective).toBe(180);
    expect(view.bestCell).toBe(2);
  });

  it("marks the best cell inexact when the improvement was inside the batch", () => {
    // last_outcome is the batch's final scenario. When its objective is not
    // the new best, the improvement happened at some earlier scenario the
    // stream never named, and the cell is an approximation.
    const exact = deriveSweep([sweepMessage(1.2, 1.0, 180)]);
    expect(exact.bestCellExact).toBe(true);

    const batched = deriveSweep([
      progress({
        primary_metric: 400,
        payload: {
          scenarios_done: 20,
          scenarios_total: 72,
          last_scenario: { capacity: 1.0, demand: 1.2, unit_cost: 1.0 },
          last_outcome: { objective: 180 },
        },
      }),
    ]);
    expect(batched.bestObjective).toBe(400);
    expect(batched.bestCellExact).toBe(false);
  });

  it("ignores a null primary_metric rather than treating it as a value", () => {
    const view = deriveSweep([sweepMessage(1.2, 0.9, 180), sweepMessage(1.5, 0.9, 0, { primary_metric: null })]);
    expect(view.bestObjective).toBe(180);
    expect(view.head).toBe(3);
  });

  it("keeps the last reported percent when a later message reports null", () => {
    const view = deriveSweep([
      sweepMessage(0.8, 0.9, 10, { percent_complete: 25 }),
      sweepMessage(1.0, 0.9, 20, { percent_complete: null }),
    ]);
    expect(view.percent).toBe(25);
  });

  it("carries the last scenario and outcome through for the readout", () => {
    const view = deriveSweep([sweepMessage(1.5, 1.1, 210)]);
    expect(view.lastScenario).toEqual({ capacity: 1.1, demand: 1.5, unit_cost: 1 });
    expect(view.lastOutcome?.["objective"]).toBe(210);
    expect(view.scenariosDone).toBe(10);
    expect(view.scenariosTotal).toBe(72);
  });
});

describe("objectivePoints", () => {
  it("sorts by scenario index and drops rows missing either axis", () => {
    const points = objectivePoints([
      result([
        { scenario_index: 4, objective: 40 },
        { scenario_index: 1, objective: 10 },
        { scenario_index: 2, objective: null },
        { objective: 99 },
      ]),
    ]);
    expect(points).toEqual([
      { scenario_index: 1, objective: 10 },
      { scenario_index: 4, objective: 40 },
    ]);
  });

  it("does not double-plot a scenario delivered twice", () => {
    // Backfill and the live stream can both deliver the same result message.
    const rows = [{ scenario_index: 3, objective: 30 }];
    expect(objectivePoints([result(rows), result(rows, { seq: 120 })])).toHaveLength(1);
  });
});
