import { describe, expect, it } from "vitest";

import type { ProgressMessage } from "@/lib/envelope";
import {
  METRIC_DIRECTION,
  baselineVerdict,
  metricTrend,
} from "./metric";
import { stepSeries, trainingSummary } from "./series";

const BATCHES = 6;
const EPOCHS = 3;

function sample(
  seq: number,
  overrides: {
    level?: "epoch" | "batch";
    epoch?: number;
    batch?: number;
    accuracy?: number | null;
    best?: number | null;
    baseline?: number;
    trainLoss?: number;
    valLoss?: number;
    macroF1?: number;
    payload?: Record<string, unknown>;
  } = {},
): ProgressMessage {
  const epoch = overrides.epoch ?? 0;
  const batch = overrides.batch ?? 0;
  const steps = epoch * BATCHES + batch + 1;
  return {
    type: "progress",
    run_id: "run-nn",
    seq,
    ts: 1_700_000_000_000 + seq,
    elapsed_seconds: seq * 0.05,
    percent_complete: (100 * steps) / (BATCHES * EPOCHS),
    primary_metric: overrides.accuracy === undefined ? 0.6 : overrides.accuracy,
    primary_metric_label: "val_accuracy",
    payload: overrides.payload ?? {
      level: overrides.level ?? "batch",
      epoch,
      epochs_total: EPOCHS,
      batch,
      batches_per_epoch: BATCHES,
      train_loss: overrides.trainLoss ?? 0.9,
      val_loss: overrides.valLoss ?? 0.85,
      macro_f1: overrides.macroF1 ?? 0.5,
      grad_norm: 1.2,
      learning_rate: 0.01,
      best_val_accuracy: overrides.best === undefined ? null : overrides.best,
      baseline_accuracy: overrides.baseline ?? 0.55,
      device: "cpu",
      data_synthetic: false,
    },
  };
}

/** One epoch's worth of the real emission order: batch, batch, epoch. */
function epochBlock(epoch: number, startSeq: number, best: number | null): ProgressMessage[] {
  return [
    sample(startSeq, { level: "batch", epoch, batch: 1, accuracy: 0.58, best }),
    sample(startSeq + 1, { level: "batch", epoch, batch: 3, accuracy: 0.61, best }),
    sample(startSeq + 2, {
      level: "epoch",
      epoch,
      batch: BATCHES - 1,
      accuracy: 0.64,
      best: 0.64,
    }),
  ];
}

describe("neural_net metric direction", () => {
  it("is higher-is-better", () => {
    expect(METRIC_DIRECTION).toBe("higher-is-better");
  });

  it("calls a rising val_accuracy an improvement", () => {
    expect(metricTrend(0.41, 0.62)).toBe("improved");
  });

  it("calls a falling val_accuracy a regression", () => {
    expect(metricTrend(0.62, 0.41)).toBe("worsened");
  });

  it("treats a null metric as unknown", () => {
    expect(metricTrend(null, 0.6)).toBe("unknown");
    expect(metricTrend(0.6, Number.NaN)).toBe("unknown");
  });
});

describe("baselineVerdict", () => {
  it("is the second, separate question: better than a constant function?", () => {
    // A run can be improving steadily and still not beat the baseline. That
    // is exactly the reading `baseline_accuracy` exists to make possible.
    expect(metricTrend(0.5, 0.548)).toBe("improved");
    expect(baselineVerdict(0.548, 0.55)).toBe("matching");
  });

  it("calls a real lift a real lift", () => {
    expect(baselineVerdict(0.67, 0.55)).toBe("beating");
  });

  it("does not treat a fraction of a point as a result", () => {
    expect(baselineVerdict(0.5551, 0.55)).toBe("matching");
  });

  it("says so when the model is worse than predicting the majority class", () => {
    expect(baselineVerdict(0.42, 0.55)).toBe("below");
  });

  it("is unknown, not zero, with no baseline in the payload", () => {
    expect(baselineVerdict(0.67, null)).toBe("unknown");
  });
});

describe("the two interleaved progress levels", () => {
  it("gives every message its own x, so points do not stack on one epoch", () => {
    // The whole reason the axis is not `epoch`: three messages share epoch 0
    // and would otherwise be three y values at one x.
    const points = stepSeries(epochBlock(0, 1, null));
    expect(points.map((p) => p.epoch)).toEqual([0, 0, 0]);
    expect(points.map((p) => p.step)).toEqual([2, 4, 6]);
  });

  it("keeps the step counter monotonic across an epoch boundary", () => {
    const points = stepSeries([...epochBlock(0, 1, null), ...epochBlock(1, 4, 0.64)]);
    const steps = points.map((p) => p.step);
    expect(steps).toEqual([...steps].sort((a, b) => a - b));
    expect(new Set(steps).size).toBe(steps.length);
  });

  it("keeps batch-level messages rather than filtering them away", () => {
    const summary = trainingSummary([...epochBlock(0, 1, null), ...epochBlock(1, 4, 0.64)]);
    expect(summary.points).toHaveLength(6);
    expect(summary.epochPoints).toHaveLength(2);
    expect(summary.batchCount).toBe(4);
  });

  it("tags each point with the level it arrived at", () => {
    expect(stepSeries(epochBlock(0, 1, null)).map((p) => p.level)).toEqual([
      "batch",
      "batch",
      "epoch",
    ]);
  });

  it("falls back to arrival order when the counters are missing", () => {
    // Payload drift, not an expected case — but stacking every point at
    // x = 0 would be a silently wrong chart rather than a visibly odd one.
    const points = stepSeries([
      sample(1, { payload: { level: "batch" } }),
      sample(2, { payload: { level: "epoch" } }),
    ]);
    expect(points.map((p) => p.step)).toEqual([1, 2]);
    expect(points.map((p) => p.epoch)).toEqual([null, null]);
  });
});

describe("best_val_accuracy during the first epoch", () => {
  it("is null, not zero, before any epoch has finished", () => {
    // Checkpointing only updates at epoch end, so the first epoch's batch
    // samples genuinely have no best yet. Rendering that as 0 would draw the
    // best-checkpoint line along the floor.
    const firstEpochSoFar = [
      sample(1, { level: "batch", epoch: 0, batch: 1, best: null }),
      sample(2, { level: "batch", epoch: 0, batch: 3, best: null }),
    ];
    expect(stepSeries(firstEpochSoFar).map((p) => p.bestValAccuracy)).toEqual([null, null]);
    expect(trainingSummary(firstEpochSoFar).bestValAccuracy).toBeNull();
  });

  it("becomes a real number once the first epoch closes", () => {
    const summary = trainingSummary(epochBlock(0, 1, null));
    expect(summary.bestValAccuracy).toBe(0.64);
  });

  it("survives a later message that happens to omit it", () => {
    const summary = trainingSummary([
      ...epochBlock(0, 1, null),
      sample(10, { payload: { level: "batch", epoch: 1, batch: 1, batches_per_epoch: BATCHES } }),
    ]);
    expect(summary.bestValAccuracy).toBe(0.64);
  });
});

describe("trainingSummary", () => {
  it("is empty, not broken, with no progress at all", () => {
    const summary = trainingSummary([]);
    expect(summary.points).toEqual([]);
    expect(summary.epochPoints).toEqual([]);
    expect(summary.latest).toBeNull();
    expect(summary.baselineAccuracy).toBeNull();
    expect(summary.bestValAccuracy).toBeNull();
    expect(summary.device).toBeNull();
  });

  it("carries the device and the data provenance through", () => {
    const summary = trainingSummary(epochBlock(0, 1, null));
    expect(summary.device).toBe("cpu");
    expect(summary.dataSynthetic).toBe(false);
  });

  it("finds the baseline even if the newest message dropped it", () => {
    const summary = trainingSummary([
      sample(1, { baseline: 0.55 }),
      sample(2, { payload: { level: "batch", epoch: 0, batch: 2, batches_per_epoch: BATCHES } }),
    ]);
    expect(summary.baselineAccuracy).toBe(0.55);
  });

  it("keeps a null percent_complete as null", () => {
    const message = sample(1);
    message.percent_complete = null;
    expect(stepSeries([message])[0]?.percent).toBeNull();
  });
});
