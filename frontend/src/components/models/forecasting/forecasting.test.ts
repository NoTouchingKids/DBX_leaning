import { describe, expect, it } from "vitest";

import type { ProgressMessage, ResultMessage } from "@/lib/envelope";
import { METRIC_DIRECTION, metricTrend } from "./metric";
import { epochSeries, forecastReveal, trainingSummary } from "./series";

function progress(
  seq: number,
  overrides: {
    epoch?: number;
    trainLoss?: number;
    valLoss?: number | null;
    bestValLoss?: number;
    percent?: number | null;
    synthetic?: boolean | null;
    payload?: Record<string, unknown>;
  } = {},
): ProgressMessage {
  const epoch = overrides.epoch ?? 0;
  return {
    type: "progress",
    run_id: "run-abc",
    seq,
    ts: 1_700_000_000_000 + seq,
    elapsed_seconds: seq * 0.1,
    percent_complete: overrides.percent === undefined ? 100 * (epoch + 1) / 40 : overrides.percent,
    primary_metric: overrides.valLoss === undefined ? 0.5 : overrides.valLoss,
    primary_metric_label: "val_loss",
    payload: overrides.payload ?? {
      epoch,
      epochs_total: 40,
      train_loss: overrides.trainLoss ?? 0.6,
      best_val_loss: overrides.bestValLoss ?? 0.5,
      learning_rate: 0.01,
      data_synthetic: overrides.synthetic ?? false,
    },
  };
}

function result(
  seq: number,
  rows: Array<Record<string, unknown>>,
  overrides: { rowCount?: number; final?: boolean; chunk?: number } = {},
): ResultMessage {
  return {
    type: "result",
    run_id: "run-abc",
    seq,
    ts: 1_700_000_000_000 + seq,
    preview: rows,
    row_count: overrides.rowCount ?? rows.length,
    fetch_hint: { table: "results_forecasting" },
    chunk_index: overrides.chunk ?? 0,
    final: overrides.final ?? true,
  };
}

describe("forecasting metric direction", () => {
  it("is lower-is-better", () => {
    expect(METRIC_DIRECTION).toBe("lower-is-better");
  });

  it("calls a falling val_loss an improvement", () => {
    expect(metricTrend(0.62, 0.41)).toBe("improved");
  });

  it("calls a rising val_loss a regression", () => {
    // The same pair of numbers is an IMPROVEMENT for neural_net. See
    // ../neural_net/polarity.test.ts, which asserts the two disagree.
    expect(metricTrend(0.41, 0.62)).toBe("worsened");
  });

  it("treats a null metric as unknown rather than as zero", () => {
    // The server sanitises NaN and +/-Infinity to null, which is what a
    // diverged run reports. Reading that as "improved to 0" would be the
    // worst possible interpretation.
    expect(metricTrend(0.4, null)).toBe("unknown");
    expect(metricTrend(null, 0.4)).toBe("unknown");
    expect(metricTrend(0.4, Number.NaN)).toBe("unknown");
  });

  it("distinguishes flat from improved", () => {
    expect(metricTrend(0.4, 0.4)).toBe("flat");
  });
});

describe("epochSeries", () => {
  it("is empty, not broken, when the run has emitted no progress", () => {
    expect(epochSeries([])).toEqual([]);
    const summary = trainingSummary([]);
    expect(summary.points).toEqual([]);
    expect(summary.latest).toBeNull();
    expect(summary.previous).toBeNull();
    expect(summary.epochsTotal).toBeNull();
  });

  it("takes val_loss from primary_metric, not from the payload", () => {
    // gaps-and-corrections B1: `val_loss` is NOT a payload key on this model.
    // A payload key of that name is a decoy and must be ignored.
    const message = progress(1, { epoch: 0, trainLoss: 0.9, valLoss: 0.44 });
    message.payload = { ...message.payload, val_loss: 999 };
    const [point] = epochSeries([message]);
    expect(point?.valLoss).toBe(0.44);
    expect(point?.trainLoss).toBe(0.9);
  });

  it("labels the 0-based epoch as 1-based for display", () => {
    const [point] = epochSeries([progress(1, { epoch: 0 })]);
    expect(point?.epoch).toBe(0);
    expect(point?.epochLabel).toBe(1);
  });

  it("collapses two messages describing the same epoch, newest seq winning", () => {
    const points = epochSeries([
      progress(1, { epoch: 3, valLoss: 0.5 }),
      progress(9, { epoch: 3, valLoss: 0.4 }),
    ]);
    expect(points).toHaveLength(1);
    expect(points[0]?.valLoss).toBe(0.4);
  });

  it("sorts by epoch even when messages arrive out of order", () => {
    const points = epochSeries([progress(5, { epoch: 2 }), progress(1, { epoch: 0 })]);
    expect(points.map((p) => p.epoch)).toEqual([0, 2]);
  });

  it("drops a message carrying only the common envelope fields", () => {
    // A payload with no `epoch` is not this model's traffic (or the shape
    // drifted). Plotting it at x = 0 would corrupt the axis silently.
    expect(epochSeries([progress(1, { payload: {} })])).toEqual([]);
  });

  it("keeps a null primary_metric as null rather than as a plotted zero", () => {
    const points = epochSeries([progress(1, { epoch: 0, valLoss: null })]);
    expect(points[0]?.valLoss).toBeNull();
  });
});

describe("forecastReveal", () => {
  it("reports nothing rather than an empty chart when no result has arrived", () => {
    const reveal = forecastReveal([]);
    expect(reveal.points).toEqual([]);
    expect(reveal.rowCount).toBeNull();
    expect(reveal.complete).toBe(false);
  });

  it("keeps row_count 0 distinguishable from 'no result yet'", () => {
    // results() returns [] when the run never kept a checkpoint. That is a
    // real outcome — SUCCEEDED with no forecast in it — not a missing chart.
    const reveal = forecastReveal([result(10, [], { rowCount: 0 })]);
    expect(reveal.rowCount).toBe(0);
    expect(reveal.complete).toBe(true);
  });

  it("builds a constant-width band from val_mae", () => {
    const reveal = forecastReveal([
      result(10, [
        { step: 0, forecast: 100, val_mae: 4, val_rmse: 6, epochs_trained: 40 },
        { step: 1, forecast: 130, val_mae: 4, val_rmse: 6, epochs_trained: 40 },
      ]),
    ]);
    expect(reveal.valMae).toBe(4);
    expect(reveal.points[0]?.band).toEqual([96, 104]);
    expect(reveal.points[1]?.band).toEqual([126, 134]);
  });

  it("leaves the band null when the preview carries no val_mae", () => {
    const reveal = forecastReveal([result(10, [{ step: 0, forecast: 100 }])]);
    expect(reveal.points[0]?.band).toBeNull();
  });

  it("orders by step and ignores rows missing either preview axis", () => {
    const reveal = forecastReveal([
      result(10, [
        { step: 2, forecast: 3 },
        { forecast: 9 },
        { step: 0, forecast: 1 },
        { step: 1 },
      ]),
    ]);
    expect(reveal.points.map((p) => p.step)).toEqual([0, 2]);
  });

  it("does not claim completeness before a final result", () => {
    const reveal = forecastReveal([
      result(10, [{ step: 0, forecast: 1 }], { final: false }),
    ]);
    expect(reveal.complete).toBe(false);
  });
});

describe("trainingSummary", () => {
  it("surfaces data_synthetic so a fallback run can be badged live", () => {
    expect(trainingSummary([progress(1, { synthetic: true })]).dataSynthetic).toBe(true);
    expect(trainingSummary([progress(1, { synthetic: false })]).dataSynthetic).toBe(false);
    expect(trainingSummary([progress(1, { payload: { epoch: 0 } })]).dataSynthetic).toBeNull();
  });

  it("exposes the previous point so a trend can be computed at all", () => {
    const summary = trainingSummary([
      progress(1, { epoch: 0, valLoss: 0.9 }),
      progress(2, { epoch: 1, valLoss: 0.5 }),
    ]);
    expect(metricTrend(summary.previous?.valLoss, summary.latest?.valLoss)).toBe("improved");
  });
});
