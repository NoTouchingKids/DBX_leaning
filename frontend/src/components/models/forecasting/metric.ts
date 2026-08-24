/**
 * Which way is better, for THIS model, stated once and in this model's own
 * directory.
 *
 * `forecasting`'s `primary_metric` is `val_loss`: a mean squared error, so it
 * improves DOWNWARD. `neural_net`'s is `val_accuracy` and improves UPWARD.
 * The two are deliberately not sharing an implementation — a single
 * "the number went up, so colour it green" helper renders a training run that
 * is working as one that is failing, and that bug is invisible in a
 * screenshot. If this ever is hoisted, direction must be an argument, never a
 * default.
 *
 * `../neural_net/metric.ts` is the mirror image, and the two test files assert
 * that the same pair of numbers gets opposite verdicts.
 */

export const METRIC_DIRECTION = "lower-is-better" as const;

/** The label the model itself puts on `primary_metric_label`. */
export const METRIC_LABEL = "val_loss";

export type Trend = "improved" | "worsened" | "flat" | "unknown";

/**
 * Compare two consecutive `primary_metric` readings.
 *
 * `null` is a real value on the wire (the server sanitises NaN and ±Infinity
 * to null), so it maps to `unknown` rather than to zero — a run whose loss
 * diverged reports null, and calling that "improved" would be the worst
 * possible reading of it.
 */
export function metricTrend(
  previous: number | null | undefined,
  latest: number | null | undefined,
): Trend {
  if (
    previous === null ||
    previous === undefined ||
    latest === null ||
    latest === undefined ||
    !Number.isFinite(previous) ||
    !Number.isFinite(latest)
  ) {
    return "unknown";
  }
  if (latest === previous) return "flat";
  // Lower is better here. This line is the whole point of the file.
  return latest < previous ? "improved" : "worsened";
}

export const TREND_CLASS: Record<Trend, string> = {
  improved: "text-good",
  worsened: "text-bad",
  flat: "text-dim",
  unknown: "text-faint",
};

/** Prose, because an arrow alone does not say which direction is good. */
export const TREND_LABEL: Record<Trend, string> = {
  improved: "falling",
  worsened: "rising",
  flat: "unchanged",
  unknown: "no reading",
};
