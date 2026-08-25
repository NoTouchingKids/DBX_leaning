/**
 * The one test that exists because of a bug that would be invisible.
 *
 * `neural_net`'s `primary_metric` is `val_accuracy` and improves UPWARD.
 * `forecasting`'s is a mean squared error and improves DOWNWARD. If the two
 * views ever come to share a "the number went up, so colour it green"
 * helper — by refactoring, by copy-paste, or by someone hoisting the
 * "duplicated" file — one of them starts rendering a working training run as
 * a failing one, and the page will look completely normal while doing it.
 *
 * So: feed both directories' trend functions the same pair of numbers and
 * assert they disagree. It fails the moment the two are unified.
 *
 * It lives here rather than in a shared test file for the same reason the
 * implementations do: there is no shared location that both models are
 * allowed to depend on for anything that carries polarity.
 */

import { describe, expect, it } from "vitest";

import * as forecasting from "../forecasting/metric";
import * as neuralNet from "./metric";

const LOWER = 0.41;
const HIGHER = 0.62;

describe("the two models' metrics point opposite ways", () => {
  it("declares opposite directions", () => {
    expect(forecasting.METRIC_DIRECTION).toBe("lower-is-better");
    expect(neuralNet.METRIC_DIRECTION).toBe("higher-is-better");
    expect(forecasting.METRIC_DIRECTION).not.toBe(neuralNet.METRIC_DIRECTION);
  });

  it("reads the same rise as opposite outcomes", () => {
    expect(forecasting.metricTrend(LOWER, HIGHER)).toBe("worsened");
    expect(neuralNet.metricTrend(LOWER, HIGHER)).toBe("improved");
  });

  it("reads the same fall as opposite outcomes", () => {
    expect(forecasting.metricTrend(HIGHER, LOWER)).toBe("improved");
    expect(neuralNet.metricTrend(HIGHER, LOWER)).toBe("worsened");
  });

  it("colours the same rise good on one model and bad on the other", () => {
    // The rendered consequence, asserted directly — a direction constant that
    // nothing reads would not catch this.
    const rise = { forecasting: forecasting.metricTrend(LOWER, HIGHER), neuralNet: neuralNet.metricTrend(LOWER, HIGHER) };
    expect(forecasting.TREND_CLASS[rise.forecasting]).toBe("text-bad");
    expect(neuralNet.TREND_CLASS[rise.neuralNet]).toBe("text-good");
  });

  it("agrees only on the cases that carry no direction", () => {
    expect(forecasting.metricTrend(0.5, 0.5)).toBe(neuralNet.metricTrend(0.5, 0.5));
    expect(forecasting.metricTrend(null, 0.5)).toBe(neuralNet.metricTrend(null, 0.5));
  });

  it("uses opposite prose, so an arrow alone never has to carry it", () => {
    // "rising" is the good word for one and the bad word for the other.
    expect(forecasting.TREND_LABEL.improved).toBe("falling");
    expect(neuralNet.TREND_LABEL.improved).toBe("rising");
  });
});
