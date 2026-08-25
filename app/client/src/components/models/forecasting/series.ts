/**
 * Snapshot -> chartable arrays. Pure, so the awkward cases are testable
 * without mounting a chart that jsdom cannot lay out anyway.
 *
 * Two things here are not obvious from the payload interface:
 *
 *  1. `val_loss` is NOT a payload key. It is `primary_metric` (label
 *     `"val_loss"`); the payload carries `train_loss` and `best_val_loss`.
 *     Verified in `job/models/forecasting/model.py::_progress`. Earlier design
 *     notes describe the chart as "train_loss/val_loss over epoch" and are
 *     wrong about where one of the two series comes from.
 *  2. `epoch` is 0-BASED — `percent_complete` is `100*(epoch+1)/epochs`, so
 *     the first message reports epoch 0. Charts label `epoch + 1` because a
 *     "0 / 40" readout reads as "nothing has happened".
 */

import type { ProgressMessage, ResultMessage } from "@/lib/envelope";
import type { ForecastingProgressPayload } from "@/lib/models";
import { payloadOf } from "../contract";

/** One training epoch, from one `progress` message. */
export interface EpochPoint {
  /** 0-based, as emitted. */
  epoch: number;
  /** For axis labels and tooltips: 1-based. */
  epochLabel: number;
  trainLoss: number | null;
  /** From `primary_metric`, not from `payload`. See the header. */
  valLoss: number | null;
  bestValLoss: number | null;
  learningRate: number | null;
  elapsed: number;
}

function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * One point per epoch, ascending.
 *
 * Collapsed by `epoch`, highest `seq` winning. `RunStore` already de-dupes by
 * `seq`, which covers a hydrate overlapping live traffic; this covers the
 * other case it cannot — two DIFFERENT messages reporting the same epoch. A
 * chart keyed on epoch draws a vertical spike through both of them, so the
 * axis key gets to be unique by construction rather than by assumption.
 */
export function epochSeries(progress: readonly ProgressMessage[]): EpochPoint[] {
  const byEpoch = new Map<number, { seq: number; point: EpochPoint }>();

  for (const message of progress) {
    const payload = payloadOf<ForecastingProgressPayload>(message);
    const epoch = asNumber(payload.epoch);
    // A message with no epoch is not from this model (or the payload shape
    // drifted). Dropping it is better than plotting it at x = 0.
    if (epoch === null) continue;

    const existing = byEpoch.get(epoch);
    if (existing !== undefined && existing.seq >= message.seq) continue;

    byEpoch.set(epoch, {
      seq: message.seq,
      point: {
        epoch,
        epochLabel: epoch + 1,
        trainLoss: asNumber(payload.train_loss),
        valLoss: asNumber(message.primary_metric),
        bestValLoss: asNumber(payload.best_val_loss),
        learningRate: asNumber(payload.learning_rate),
        elapsed: message.elapsed_seconds,
      },
    });
  }

  return [...byEpoch.values()]
    .map((entry) => entry.point)
    .sort((a, b) => a.epoch - b.epoch);
}

export interface TrainingSummary {
  points: EpochPoint[];
  /** `epochs_total` as the model last reported it — the plan, not the
   *  outcome. A run that diverges or is cancelled stops short of it. */
  epochsTotal: number | null;
  /** Best val_loss seen, straight from the payload rather than recomputed:
   *  the model's own early-stopping tracker is the authority on which
   *  checkpoint `results()` will be written from. */
  bestValLoss: number | null;
  latest: EpochPoint | null;
  previous: EpochPoint | null;
  /** `null` when no message has carried it. `false` means real catalog data. */
  dataSynthetic: boolean | null;
}

export function trainingSummary(progress: readonly ProgressMessage[]): TrainingSummary {
  const points = epochSeries(progress);
  const last = progress.at(-1);
  const payload = payloadOf<ForecastingProgressPayload>(last);

  const synthetic = payload.data_synthetic;

  return {
    points,
    epochsTotal: asNumber(payload.epochs_total),
    bestValLoss: asNumber(payload.best_val_loss),
    latest: points.at(-1) ?? null,
    previous: points.at(-2) ?? null,
    dataSynthetic: typeof synthetic === "boolean" ? synthetic : null,
  };
}

/* ------------------------------------------------------------------ *
 * results() — the forecast reveal
 * ------------------------------------------------------------------ */

export interface ForecastPoint {
  step: number;
  forecast: number;
  /** `[low, high]` for a recharts range Area, or null when the run wrote no
   *  `val_mae`. Constant width by construction — see `bandIsConstant`. */
  band: [number, number] | null;
}

export interface ForecastReveal {
  points: ForecastPoint[];
  valMae: number | null;
  valRmse: number | null;
  epochsTrained: number | null;
  /** Summed across chunks. `0` is meaningful and must not render as "no data
   *  yet": `results()` returns [] when the run never completed an epoch, so a
   *  SUCCEEDED run with row_count 0 is a real, reportable outcome. */
  rowCount: number | null;
  /** Whether a `final: true` result has arrived. Forecasting emits exactly
   *  one result chunk, but the flag is the contract, not the count. */
  complete: boolean;
  dataSynthetic: boolean | null;
}

/**
 * `preview_axes = ("step", "forecast")` on the model, so those are the two
 * keys a preview row is guaranteed to carry. Everything else here is
 * best-effort: the preview is LTTB-downsampled server-side, and a
 * downsampler is free to drop rows, never to invent columns.
 */
export function forecastReveal(results: readonly ResultMessage[]): ForecastReveal {
  const points: ForecastPoint[] = [];
  let valMae: number | null = null;
  let valRmse: number | null = null;
  let epochsTrained: number | null = null;
  let rowCount: number | null = null;
  let complete = false;
  let dataSynthetic: boolean | null = null;

  for (const message of results) {
    rowCount = (rowCount ?? 0) + message.row_count;
    if (message.final) complete = true;

    for (const row of message.preview) {
      const step = asNumber(row["step"]);
      const forecast = asNumber(row["forecast"]);
      if (step === null || forecast === null) continue;

      valMae = asNumber(row["val_mae"]) ?? valMae;
      valRmse = asNumber(row["val_rmse"]) ?? valRmse;
      epochsTrained = asNumber(row["epochs_trained"]) ?? epochsTrained;
      if (typeof row["data_synthetic"] === "boolean") {
        dataSynthetic = row["data_synthetic"];
      }

      points.push({ step, forecast, band: null });
    }
  }

  points.sort((a, b) => a.step - b.step);

  // The band is +/- val_mae, which is the SAME number on every row — the
  // model writes one held-out MAE and repeats it. So this is a constant-width
  // ribbon and must be captioned as one, not as a fanning prediction
  // interval. The model emits no interval; drawing a fan would invent one.
  if (valMae !== null) {
    for (const point of points) {
      point.band = [point.forecast - valMae, point.forecast + valMae];
    }
  }

  return { points, valMae, valRmse, epochsTrained, rowCount, complete, dataSynthetic };
}
