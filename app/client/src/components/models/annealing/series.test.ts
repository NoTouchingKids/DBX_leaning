/**
 * The arithmetic, not the markup.
 *
 * What can actually be wrong in this view is numeric: a "best" line quietly
 * recomputed as a running max, a heat value that saturates so the cooling
 * animation stops meaning anything, an over-capacity walk that acquires an
 * alarm tone, or a chart that draws an axis over no data. Those are all here.
 */

import { describe, expect, it } from "vitest";

import type { ProgressMessage } from "@/lib/envelope";

import {
  buildPoints,
  coolingSeries,
  deriveHeat,
  emptyProgressReason,
  heatPhase,
  shiftUsage,
  traceDomain,
  traceSeries,
} from "./series";

function progress(
  seq: number,
  primaryMetric: number | null,
  payload: Record<string, unknown>,
  overrides: Partial<ProgressMessage> = {},
): ProgressMessage {
  return {
    type: "progress",
    run_id: "run-000000000001",
    seq,
    ts: 1_700_000_000_000 + seq,
    elapsed_seconds: seq * 0.5,
    percent_complete: null,
    primary_metric: primaryMetric,
    primary_metric_label: "best_fare",
    payload,
    ...overrides,
  };
}

/** A payload shaped exactly like `AnnealingModel._progress` emits one. */
function annealingPayload(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    iteration: 1000,
    iterations_total: 30_000,
    temperature: 12.5,
    current_objective: 300,
    current_value: 300,
    current_weight: 400,
    capacity: 480,
    feasible: true,
    acceptance_rate: 0.62,
    accepted_total: 620,
    items_selected: 21,
    ...over,
  };
}

describe("buildPoints", () => {
  it("reads every payload key the model emits", () => {
    const [point] = buildPoints([progress(4, 512.5, annealingPayload())]);

    expect(point).toMatchObject({
      seq: 4,
      iteration: 1000,
      iterationsTotal: 30_000,
      temperature: 12.5,
      current: 300,
      currentWeight: 400,
      capacity: 480,
      feasible: true,
      acceptanceRate: 0.62,
      itemsSelected: 21,
      best: 512.5,
    });
  });

  it("treats an absent or non-finite payload field as absent, not as zero", () => {
    const [point] = buildPoints([
      progress(1, null, { iteration: 5, temperature: Number.NaN }),
    ]);

    expect(point?.temperature).toBeNull();
    expect(point?.current).toBeNull();
    expect(point?.feasible).toBeNull();
    expect(point?.best).toBeNull();
  });

  it("orders by seq, because backfill delivers below the high-water mark", () => {
    const points = buildPoints([
      progress(9, 3, annealingPayload({ iteration: 3000 })),
      progress(3, 1, annealingPayload({ iteration: 1000 })),
      progress(6, 2, annealingPayload({ iteration: 2000 })),
    ]);

    expect(points.map((p) => p.iteration)).toEqual([1000, 2000, 3000]);
  });
});

describe("traceSeries — current against best", () => {
  it("keeps the current objective exactly as reported, dips and all", () => {
    // The point of the model: the walk accepts uphill moves, so `current`
    // falls below where it has already been. Anything that repaired this into
    // a monotone series would be deleting the run's actual behaviour.
    const currents = [300, 260, 240, 290, 210, 330];
    const series = traceSeries(
      buildPoints(
        currents.map((current, i) =>
          progress(
            i,
            400 + i, // best, monotonic
            annealingPayload({ iteration: (i + 1) * 1000, current_objective: current }),
          ),
        ),
      ),
    );

    expect(series.map((p) => p.current)).toEqual(currents);
  });

  it("never clamps the current objective up to the best fare", () => {
    const series = traceSeries(
      buildPoints([progress(1, 900, annealingPayload({ current_objective: -120 }))]),
    );

    expect(series[0]?.current).toBe(-120);
    expect(series[0]?.best).toBe(900);
  });

  it("takes best straight from primary_metric rather than recomputing it", () => {
    // If the model ever stopped being monotonic, this view must show that
    // rather than paper over it with a running max.
    const series = traceSeries(
      buildPoints([
        progress(1, 500, annealingPayload({ iteration: 1000 })),
        progress(2, 400, annealingPayload({ iteration: 2000 })),
      ]),
    );

    expect(series.map((p) => p.best)).toEqual([500, 400]);
  });

  it("marks over-shift points on a separate key and leaves the rest null", () => {
    const series = traceSeries(
      buildPoints([
        progress(1, 500, annealingPayload({ iteration: 1000, feasible: true })),
        progress(
          2,
          500,
          annealingPayload({ iteration: 2000, feasible: false, current_objective: 180 }),
        ),
      ]),
    );

    expect(series[0]?.currentOverShift).toBeNull();
    expect(series[1]?.currentOverShift).toBe(180);
  });

  it("drops messages with no iteration to plot against", () => {
    const series = traceSeries(buildPoints([progress(1, 500, {})]));
    expect(series).toEqual([]);
  });
});

describe("traceDomain", () => {
  it("is null when there is nothing to plot", () => {
    expect(traceDomain([])).toBeNull();
  });

  it("pads a single point so the axis does not collapse", () => {
    const domain = traceDomain([
      { iteration: 1000, current: 1, best: 1, currentOverShift: null },
    ]);

    expect(domain).not.toBeNull();
    expect(domain?.[0]).toBeLessThan(1000);
    expect(domain?.[1]).toBeGreaterThan(1000);
  });

  it("spans the data when there is more than one point", () => {
    expect(
      traceDomain([
        { iteration: 1000, current: 1, best: 1, currentOverShift: null },
        { iteration: 9000, current: 2, best: 2, currentOverShift: null },
      ]),
    ).toEqual([1000, 9000]);
  });
});

describe("coolingSeries", () => {
  it("keeps only points a log axis can take", () => {
    const series = coolingSeries(
      buildPoints([
        progress(1, 1, annealingPayload({ iteration: 1000, temperature: 12 })),
        progress(2, 1, annealingPayload({ iteration: 2000, temperature: 0 })),
        progress(3, 1, annealingPayload({ iteration: 3000, temperature: -1 })),
        progress(4, 1, { iteration: 4000 }),
      ]),
    );

    expect(series).toEqual([{ iteration: 1000, temperature: 12, acceptanceRate: 0.62 }]);
  });
});

describe("deriveHeat", () => {
  /** The model's own schedule: geometric from `start` to `end` over `total`
   *  iterations, reported one iteration behind as `_progress` does. */
  function schedule(start: number, end: number, total: number, reportAt: number[]) {
    return reportAt.map((reported, i) => {
      const trueIteration = reported - 1;
      const temperature =
        start * (end / start) ** (trueIteration / (total - 1));
      return progress(
        i,
        1,
        annealingPayload({
          iteration: reported,
          iterations_total: total,
          temperature,
        }),
      );
    });
  }

  it("is null before any temperature has been reported", () => {
    expect(deriveHeat([])).toBeNull();
    expect(deriveHeat(buildPoints([progress(1, 1, {})]))).toBeNull();
  });

  it("is fully hot on the first observation, when the schedule is unknown", () => {
    expect(deriveHeat(buildPoints(schedule(10, 0.01, 30_000, [1000])))).toBe(1);
  });

  it("recovers the cooling schedule from two observations", () => {
    // Two points determine the whole geometric line, including the end
    // temperature the payload never carries — which is why this is fitted
    // rather than hardcoded to END_TEMPERATURE_RATIO.
    const total = 30_000;
    const points = buildPoints(schedule(10, 0.01, total, [1000, 2000]));

    expect(deriveHeat(points)).toBeCloseTo(1 - 2000 / (total - 1), 5);
  });

  it("reaches zero at the end of the plan", () => {
    const total = 30_000;
    const reportAt = Array.from({ length: 30 }, (_, i) => (i + 1) * 1000);
    const points = buildPoints(schedule(10, 0.01, total, reportAt));

    expect(deriveHeat(points)).toBeCloseTo(0, 3);
  });

  it("tracks the whole schedule monotonically downward", () => {
    const total = 30_000;
    const reportAt = Array.from({ length: 30 }, (_, i) => (i + 1) * 1000);
    const all = buildPoints(schedule(10, 0.01, total, reportAt));
    const heats = reportAt.map((_, i) => deriveHeat(all.slice(0, i + 1)) ?? Number.NaN);

    for (let i = 1; i < heats.length; i += 1) {
      expect(heats[i]).toBeLessThan(heats[i - 1] ?? Number.NaN);
    }
    expect(heats.at(-1)).toBeLessThan(0.05);
  });

  it("normalises against the run's own bounds, not a hardcoded ratio", () => {
    // `start_temperature` and `end_temperature` are both config-overridable
    // and both derived from the fare distribution when they are not set, so
    // there is no fixed temperature that means "cold". A shallow schedule and
    // a steep one at the same point in the plan are equally far along, and a
    // view that assumed END_TEMPERATURE_RATIO would call the shallow one hot
    // for its whole life.
    const total = 30_000;
    const shallow = buildPoints(schedule(10, 5, total, [1000, 29_000]));
    const steep = buildPoints(schedule(10, 0.001, total, [1000, 29_000]));

    expect(deriveHeat(shallow)).toBeCloseTo(deriveHeat(steep) ?? Number.NaN, 6);
    expect(deriveHeat(shallow)).toBeLessThan(0.05);
  });

  it("reports hot forever when the temperature never falls", () => {
    const flat = buildPoints([
      progress(1, 1, annealingPayload({ iteration: 1000, temperature: 4 })),
      progress(2, 1, annealingPayload({ iteration: 2000, temperature: 4 })),
    ]);

    expect(deriveHeat(flat)).toBe(1);
  });
});

describe("heatPhase", () => {
  it("distinguishes no-temperature-yet from cold", () => {
    expect(heatPhase(null)).toBe("unknown");
    expect(heatPhase(0)).toBe("cold");
  });

  it("moves hot -> cooling -> cold as the run progresses", () => {
    expect(heatPhase(1)).toBe("hot");
    expect(heatPhase(0.4)).toBe("cooling");
    expect(heatPhase(0.05)).toBe("cold");
  });
});

describe("shiftUsage — feasible: false is not a fault", () => {
  it("is null when the payload has no weight or capacity", () => {
    expect(shiftUsage(null)).toBeNull();
    expect(shiftUsage(buildPoints([progress(1, 1, { iteration: 1 })])[0] ?? null)).toBeNull();
  });

  it("reads as calm and explanatory when the walk is over the shift", () => {
    const [point] = buildPoints([
      progress(
        1,
        500,
        annealingPayload({ current_weight: 512, capacity: 480, feasible: false }),
      ),
    ]);
    const usage = shiftUsage(point ?? null);

    expect(usage?.overShift).toBe(true);
    expect(usage?.overBy).toBeCloseTo(32);
    // The tone union has no `bad` or `warn` member, so this is enforced by the
    // compiler too — the assertion is here to catch someone widening it.
    expect(usage?.tone).toBe("info");
    expect(usage?.note).toContain("on purpose");
  });

  it("is neutral when the walk is inside the shift", () => {
    const [point] = buildPoints([progress(1, 500, annealingPayload())]);
    const usage = shiftUsage(point ?? null);

    expect(usage?.overShift).toBe(false);
    expect(usage?.tone).toBe("neutral");
  });
});

describe("emptyProgressReason", () => {
  it("says a finished run is finished rather than implying it is still loading", () => {
    expect(emptyProgressReason("SUCCEEDED")).toContain("finished");
    expect(emptyProgressReason("CANCELLED")).toContain("finished");
  });

  it("says a live run has not reported yet", () => {
    expect(emptyProgressReason("RUNNING")).toContain("No progress reported yet");
    expect(emptyProgressReason("QUEUED")).toContain("Waiting for the job");
  });

  it("distinguishes having no run at all", () => {
    expect(emptyProgressReason(null)).toContain("No run selected");
  });
});
