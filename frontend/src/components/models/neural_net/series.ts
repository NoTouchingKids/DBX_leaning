/**
 * Snapshot -> chartable arrays, and the decision about the two progress
 * levels.
 *
 * ## The two levels, and why the x axis is not `epoch`
 *
 * `models/neural_net/model.py` emits progress at two levels on ONE stream:
 * `level: "batch"` a couple of times per epoch (`batch_updates_per_epoch`,
 * default 2) and `level: "epoch"` once at the end of each. Both carry the
 * same payload shape. So on the default config every epoch produces three
 * messages, and a chart with `dataKey="epoch"` gets three y values at one x
 * and draws a vertical spike through each of them.
 *
 * The three options, and why this one:
 *
 *  - **Filter to `level === "epoch"`.** Throws away two thirds of the run's
 *    telemetry, and the discarded messages are not padding — each batch
 *    sample re-runs `_evaluate()`, so it is a real, independent measurement
 *    that cost the run something to produce.
 *  - **Two separate charts.** Two charts is this model's whole budget, and
 *    spending both on the same numbers at two granularities says nothing.
 *  - **One monotonic step axis, with the level marked per point.** Taken.
 *    `epoch * batches_per_epoch + batch + 1` is the model's own counter —
 *    `percent_complete` is `100 * steps / total_steps` over exactly these
 *    batch steps, which is why it advances between epoch boundaries. So the
 *    step index is not an invention; it is the x axis the model is already
 *    reporting against. Epoch-level points are then drawn with a visible dot
 *    and epoch boundaries get reference lines, so the coarser level stays
 *    legible inside the finer one instead of being averaged away.
 */

import type { ProgressMessage } from "@/lib/envelope";
import type { NeuralNetProgressPayload } from "@/lib/models";
import { payloadOf } from "../contract";

export type ProgressLevel = "epoch" | "batch" | "unknown";

export interface StepPoint {
  /** Monotonic across BOTH levels. See the header. */
  step: number;
  level: ProgressLevel;
  /** 0-based, as emitted. */
  epoch: number | null;
  /** For humans: 1-based. */
  epochLabel: number | null;
  batch: number | null;
  batchesPerEpoch: number | null;
  trainLoss: number | null;
  /** In the payload for this model, unlike forecasting. */
  valLoss: number | null;
  /** `primary_metric`. Bounded 0..1, higher is better. */
  valAccuracy: number | null;
  macroF1: number | null;
  /** NULL through the first epoch's batch samples — best-checkpoint tracking
   *  only updates at epoch end. Null means "no epoch has finished yet", not
   *  zero, and it must render as a gap rather than as a line to the floor. */
  bestValAccuracy: number | null;
  baselineAccuracy: number | null;
  gradNorm: number | null;
  learningRate: number | null;
  percent: number | null;
  elapsed: number;
  seq: number;
}

function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function levelOf(value: unknown): ProgressLevel {
  return value === "epoch" || value === "batch" ? value : "unknown";
}

/**
 * All progress points, ordered, with the level kept.
 *
 * Keyed by `seq`, not by step, and deliberately NOT collapsed by step: two
 * points can share a step legitimately here — the epoch-level message reports
 * `batch = batches - 1`, which is the same step the last batch-level sample of
 * a long epoch could land on. Both are real measurements and both are kept;
 * the recharts series is drawn in this order, so a same-x pair reads as the
 * level transition it is. `RunStore` de-dupes by `seq` upstream; the map here
 * is belt-and-braces, since this function is also called directly in tests
 * with hand-built arrays.
 */
export function stepSeries(progress: readonly ProgressMessage[]): StepPoint[] {
  const bySeq = new Map<number, StepPoint>();

  for (const [index, message] of progress.entries()) {
    if (bySeq.has(message.seq)) continue;
    const payload = payloadOf<NeuralNetProgressPayload>(message);

    const epoch = asNumber(payload.epoch);
    const batch = asNumber(payload.batch);
    const batchesPerEpoch = asNumber(payload.batches_per_epoch);

    // Fall back to arrival order when the counters are missing. That is a
    // payload-drift case, not an expected one, but a chart that silently
    // stacks every point at x = 0 is worse than one with an approximate axis.
    const step =
      epoch !== null && batch !== null && batchesPerEpoch !== null
        ? epoch * batchesPerEpoch + batch + 1
        : index + 1;

    bySeq.set(message.seq, {
      step,
      level: levelOf(payload.level),
      epoch,
      epochLabel: epoch === null ? null : epoch + 1,
      batch,
      batchesPerEpoch,
      trainLoss: asNumber(payload.train_loss),
      valLoss: asNumber(payload.val_loss),
      valAccuracy: asNumber(message.primary_metric),
      macroF1: asNumber(payload.macro_f1),
      bestValAccuracy: asNumber(payload.best_val_accuracy),
      baselineAccuracy: asNumber(payload.baseline_accuracy),
      gradNorm: asNumber(payload.grad_norm),
      learningRate: asNumber(payload.learning_rate),
      percent: message.percent_complete,
      elapsed: message.elapsed_seconds,
      seq: message.seq,
    });
  }

  return [...bySeq.values()].sort((a, b) => a.step - b.step || a.seq - b.seq);
}

export interface TrainingSummary {
  points: StepPoint[];
  /** Only the `level: "epoch"` points, for the epoch ladder and for the
   *  boundary markers on the charts. Never used as the chart's own data —
   *  see the header for why. */
  epochPoints: StepPoint[];
  batchCount: number;
  latest: StepPoint | null;
  previous: StepPoint | null;
  /** From the last message that carried one. Constant per run (it is derived
   *  from the fixed validation split), so the last reading is the reading. */
  baselineAccuracy: number | null;
  /** Null until the first epoch has finished. Genuinely null, not zero. */
  bestValAccuracy: number | null;
  epochsTotal: number | null;
  batchesPerEpoch: number | null;
  /** `cpu` / `cuda` / whatever `device` was overridden to. The only field
   *  that keeps a CPU run and a GPU run distinguishable after the fact. */
  device: string | null;
  dataSynthetic: boolean | null;
}

export function trainingSummary(progress: readonly ProgressMessage[]): TrainingSummary {
  const points = stepSeries(progress);
  const epochPoints = points.filter((point) => point.level === "epoch");
  const last = points.at(-1);
  const payload = payloadOf<NeuralNetProgressPayload>(progress.at(-1));

  const synthetic = payload.data_synthetic;

  // Scan backwards rather than reading the last point: a `null`
  // best_val_accuracy on the newest message is only meaningful while no epoch
  // has finished, and after that the field is populated on every message
  // anyway. Backwards-first-non-null is right in both regimes.
  const bestValAccuracy =
    [...points].reverse().find((point) => point.bestValAccuracy !== null)?.bestValAccuracy ??
    null;
  const baselineAccuracy =
    [...points].reverse().find((point) => point.baselineAccuracy !== null)?.baselineAccuracy ??
    null;

  return {
    points,
    epochPoints,
    batchCount: points.length - epochPoints.length,
    latest: last ?? null,
    previous: points.at(-2) ?? null,
    baselineAccuracy,
    bestValAccuracy,
    epochsTotal: asNumber(payload.epochs_total),
    batchesPerEpoch: asNumber(payload.batches_per_epoch),
    device: typeof payload.device === "string" ? payload.device : null,
    dataSynthetic: typeof synthetic === "boolean" ? synthetic : null,
  };
}
