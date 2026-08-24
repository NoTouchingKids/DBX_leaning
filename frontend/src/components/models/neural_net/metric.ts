/**
 * Which way is better, for THIS model. The mirror image of
 * `../forecasting/metric.ts`, and the reason neither is shared.
 *
 * `neural_net`'s `primary_metric` is `val_accuracy`, bounded 0..1, and it
 * improves UPWARD. `forecasting`'s is a mean squared error and improves
 * DOWNWARD. One shared "the number went up, so colour it green" helper would
 * render one of the two backwards — a training run that is working shown as
 * one that is failing — and nothing about the rendered page would look wrong.
 * So the comparison is written out, once, in each model's own directory. If
 * this is ever hoisted, direction must be a required argument with no default.
 *
 * The second, model-specific trap lives here too: accuracy on this problem is
 * meaningless without the majority-class baseline. The classes are ~55/30/15
 * on purpose, so a constant function scores ~0.55. `lift` is the number that
 * actually says whether the network learned anything.
 */

export const METRIC_DIRECTION = "higher-is-better" as const;

/** The label the model itself puts on `primary_metric_label`. */
export const METRIC_LABEL = "val_accuracy";

export type Trend = "improved" | "worsened" | "flat" | "unknown";

/**
 * Compare two consecutive `primary_metric` readings.
 *
 * Both readings are live measurements even between epochs: the batch-level
 * path calls `_evaluate()` rather than carrying a stale figure, so a flat
 * result here means the accuracy genuinely did not move, not that the same
 * number was repeated.
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
  // Higher is better here. This line is the whole point of the file.
  return latest > previous ? "improved" : "worsened";
}

export const TREND_CLASS: Record<Trend, string> = {
  improved: "text-good",
  worsened: "text-bad",
  flat: "text-dim",
  unknown: "text-faint",
};

export const TREND_LABEL: Record<Trend, string> = {
  improved: "rising",
  worsened: "falling",
  flat: "unchanged",
  unknown: "no reading",
};

export type BaselineVerdict = "beating" | "matching" | "below" | "unknown";

/**
 * Accuracy against the majority-class baseline.
 *
 * Separate from `metricTrend` on purpose: "improved since last sample" and
 * "better than a constant function" are different questions, and a run can
 * be improving steadily while still not beating the baseline.
 */
export function baselineVerdict(
  accuracy: number | null | undefined,
  baseline: number | null | undefined,
): BaselineVerdict {
  if (
    accuracy === null ||
    accuracy === undefined ||
    baseline === null ||
    baseline === undefined ||
    !Number.isFinite(accuracy) ||
    !Number.isFinite(baseline)
  ) {
    return "unknown";
  }
  // A hair above the baseline is noise, not a result. One point of accuracy
  // on a ~1000-row validation split is about ten trips.
  const lift = accuracy - baseline;
  if (lift > 0.01) return "beating";
  if (lift < -0.01) return "below";
  return "matching";
}

export const VERDICT_CLASS: Record<BaselineVerdict, string> = {
  beating: "text-good",
  matching: "text-warn",
  below: "text-bad",
  unknown: "text-faint",
};

export const VERDICT_LABEL: Record<BaselineVerdict, string> = {
  beating: "above the majority-class baseline",
  matching: "level with the majority-class baseline",
  below: "below the majority-class baseline",
  unknown: "no baseline reading",
};
