import { describe, expect, it } from "vitest";

import { payloadOf } from "@/components/models/contract";
import type { ProgressMessage, ResultMessage } from "@/lib/envelope";
import type { BayesianAbProgressPayload } from "@/lib/models";

import {
  armsFromSnapshot,
  decisionFromSnapshot,
  deriveStages,
  looksLikeInputError,
  STAGE_COUNT,
} from "./derive";
import { snapshotOf } from "./testing";

/** A progress message for stage `index`, carrying only the keys the model
 *  would actually have added by then. */
function stage(index: number, extra: Record<string, unknown> = {}): ProgressMessage {
  const arms = [
    {
      role: "A",
      label: "weekday_hours",
      trials: 1000,
      successes: 480,
      posterior_alpha: 481,
      posterior_beta: 521,
      posterior_mean: 0.48,
    },
    {
      role: "B",
      label: "weekend_hours",
      trials: 400,
      successes: 230,
      posterior_alpha: 231,
      posterior_beta: 171,
      posterior_mean: 0.574,
    },
  ];
  return {
    type: "progress",
    run_id: "run-1",
    seq: index,
    ts: 1_700_000_000_000 + index,
    elapsed_seconds: index * 0.001,
    percent_complete: (100 * index) / STAGE_COUNT,
    primary_metric: index >= 2 ? 0.999 : null,
    primary_metric_label: "prob_b_beats_a",
    payload: {
      stage: "comparison",
      stage_index: index,
      stages_total: STAGE_COUNT,
      progress_shape: "stages",
      comparison: "weekend_fare",
      outcome: "this hour's average fare was above the pooled median",
      prior: { alpha: 1, beta: 1 },
      credible_mass: 0.95,
      arms,
      ...extra,
    },
  };
}

function resultRows(rows: Array<Record<string, unknown>>): ResultMessage {
  return {
    type: "result",
    run_id: "run-1",
    seq: 99,
    ts: 1_700_000_000_100,
    preview: rows,
    row_count: rows.length,
    fetch_hint: {},
    chunk_index: 0,
    final: true,
  };
}

const ARM_ROW_A = {
  row_type: "arm",
  role: "A",
  label: "weekday_hours",
  trials: 1000,
  successes: 480,
  posterior_alpha: 481,
  posterior_beta: 521,
  posterior_mean: 0.48,
  expected_loss: 0.094,
  decision: "weekend_hours",
  conclusive: true,
  comparison: "weekend_fare",
  outcome: "above the pooled median",
  prior_alpha: 1,
  prior_beta: 1,
  credible_mass: 0.95,
  complete: true,
};

const ARM_ROW_B = {
  ...ARM_ROW_A,
  role: "B",
  label: "weekend_hours",
  trials: 400,
  successes: 230,
  posterior_alpha: 231,
  posterior_beta: 171,
  posterior_mean: 0.574,
  expected_loss: 0.0001,
};

const COMPARISON_ROW = {
  row_type: "comparison",
  role: "B_minus_A",
  label: "weekend_hours_vs_weekday_hours",
  posterior_alpha: null,
  posterior_beta: null,
  posterior_mean: 0.094,
  posterior_sd: 0.029,
  ci_low: 0.037,
  ci_high: 0.151,
  prob_beats_other: 0.9993,
  expected_loss: 0.0001,
  decision: "weekend_hours",
  conclusive: true,
  comparison: "weekend_fare",
  outcome: "above the pooled median",
  prior_alpha: 1,
  prior_beta: 1,
  credible_mass: 0.95,
  complete: true,
};

describe("the payload keys that are absent rather than null", () => {
  it("reads as undefined, not null, before its stage has run", () => {
    // The model ADDS prob_b_beats_a / expected_loss / lift / decision /
    // conclusive to the payload as their stages complete. Code that null-checks
    // these renders `undefined`; code that checks for absence does not.
    const early = payloadOf<BayesianAbProgressPayload>(stage(1));
    expect("prob_b_beats_a" in early).toBe(false);
    expect(early.prob_b_beats_a).toBeUndefined();
    expect(early.decision).toBeUndefined();

    const late = payloadOf<BayesianAbProgressPayload>(
      stage(5, { prob_b_beats_a: 0.9993, decision: "weekend_hours", conclusive: true }),
    );
    expect(late.prob_b_beats_a).toBe(0.9993);
  });

  it("turns both absence and null into one null at the view boundary", () => {
    const early = decisionFromSnapshot(snapshotOf({ progress: [stage(1)] }));
    expect(early.probBBeatsA).toBeNull();
    expect(early.decision).toBeNull();
    expect(early.lift).toBeNull();
    // What IS there from stage 1 is still there.
    expect(early.outcome).toContain("pooled median");
    expect(early.prior).toEqual({ alpha: 1, beta: 1 });
  });
});

describe("deriveStages", () => {
  it("shows all five stages done on a SUCCEEDED run that emitted no progress", () => {
    // The case this whole model view is built around: closed-form, over in
    // milliseconds, terminal status observed with an empty progress array.
    const stages = deriveStages("SUCCEEDED", snapshotOf({}));
    expect(stages.done).toBe(STAGE_COUNT);
    expect(stages.source).toBe("terminal");
    expect(stages.failedAt).toBeNull();
  });

  it("still says five, and says it came from progress, when all five arrived", () => {
    const stages = deriveStages("SUCCEEDED", snapshotOf({ progress: [stage(5)] }));
    expect(stages.done).toBe(STAGE_COUNT);
    expect(stages.source).toBe("progress");
  });

  it("points at the stage a FAILED run did not get through", () => {
    expect(deriveStages("FAILED", snapshotOf({ progress: [stage(2)] })).failedAt).toBe(3);
  });

  it("reports zero stages and stage 1 pending for a run that failed on its config", () => {
    const stages = deriveStages("FAILED", snapshotOf({}));
    expect(stages.done).toBe(0);
    expect(stages.failedAt).toBe(1);
    expect(looksLikeInputError("FAILED", stages)).toBe(true);
  });

  it("does not read a cancelled run as complete", () => {
    const stages = deriveStages("CANCELLED", snapshotOf({ progress: [stage(3)] }));
    expect(stages.done).toBe(3);
    expect(stages.failedAt).toBeNull();
    expect(looksLikeInputError("CANCELLED", stages)).toBe(false);
  });

  it("believes a complete result set when no progress survived", () => {
    const stages = deriveStages(
      "CANCELLED",
      snapshotOf({ results: [resultRows([COMPARISON_ROW])] }),
    );
    expect(stages.done).toBe(STAGE_COUNT);
    expect(stages.source).toBe("results");
  });

  it("shows nothing done before the run starts", () => {
    expect(deriveStages("STARTING", snapshotOf({})).done).toBe(0);
    expect(deriveStages(null, snapshotOf({})).done).toBe(0);
    expect(looksLikeInputError("STARTING", deriveStages("STARTING", snapshotOf({})))).toBe(false);
  });
});

describe("armsFromSnapshot", () => {
  it("prefers the live payload", () => {
    const { arms, source } = armsFromSnapshot(snapshotOf({ progress: [stage(1)] }));
    expect(source).toBe("progress");
    expect(arms.map((a) => a.label)).toEqual(["weekday_hours", "weekend_hours"]);
    expect(arms[1]?.posteriorAlpha).toBe(231);
  });

  it("falls back to the result rows when the run outran the stream", () => {
    const { arms, source } = armsFromSnapshot(
      snapshotOf({ results: [resultRows([ARM_ROW_B, ARM_ROW_A, COMPARISON_ROW])] }),
    );
    expect(source).toBe("results");
    // Sorted by role, so A is always drawn as A whatever order the rows came in.
    expect(arms.map((a) => a.role)).toEqual(["A", "B"]);
    expect(arms[0]?.label).toBe("weekday_hours");
  });

  it("reports null posteriors rather than inventing them", () => {
    const unfitted = stage(1, {
      arms: [
        { role: "A", label: "a", trials: 10, successes: 4, posterior_alpha: null, posterior_beta: null, posterior_mean: null },
        { role: "B", label: "b", trials: 10, successes: 6, posterior_alpha: null, posterior_beta: null, posterior_mean: null },
      ],
    });
    const { arms } = armsFromSnapshot(snapshotOf({ progress: [unfitted] }));
    expect(arms[0]?.posteriorAlpha).toBeNull();
  });

  it("has nothing to show when nothing was reported", () => {
    expect(armsFromSnapshot(snapshotOf({})).arms).toEqual([]);
    expect(armsFromSnapshot(snapshotOf({})).source).toBe("none");
  });
});

describe("decisionFromSnapshot", () => {
  it("reconstructs the decision from the result rows alone", () => {
    const d = decisionFromSnapshot(
      snapshotOf({ results: [resultRows([ARM_ROW_A, ARM_ROW_B, COMPARISON_ROW])] }),
    );
    expect(d.source).toBe("results");
    // The comparison row's prob_beats_other IS prob_b_beats_a — the results
    // table is a different shape from the payload, not a copy of it.
    expect(d.probBBeatsA).toBe(0.9993);
    expect(d.expectedLossA).toBe(0.094);
    expect(d.expectedLossB).toBe(0.0001);
    expect(d.lift).toEqual({ mean: 0.094, sd: 0.029, ciLow: 0.037, ciHigh: 0.151 });
    // An arm LABEL, never "A"/"B".
    expect(d.decision).toBe("weekend_hours");
    expect(d.conclusive).toBe(true);
  });

  it("handles arm rows written by a run cancelled before the comparison", () => {
    const partial = { ...ARM_ROW_A, decision: null, conclusive: null, expected_loss: null, complete: false };
    const d = decisionFromSnapshot(snapshotOf({ results: [resultRows([partial])] }));
    expect(d.probBBeatsA).toBeNull();
    expect(d.lift).toBeNull();
    expect(d.decision).toBeNull();
    expect(d.outcome).toBe("above the pooled median");
  });

  it("distinguishes inconclusive from a missing decision", () => {
    const d = decisionFromSnapshot(
      snapshotOf({ progress: [stage(5, { prob_b_beats_a: 0.7, decision: "inconclusive", conclusive: false })] }),
    );
    expect(d.decision).toBe("inconclusive");
    expect(d.conclusive).toBe(false);
  });

  it("knows nothing when nothing arrived", () => {
    expect(decisionFromSnapshot(snapshotOf({})).source).toBe("none");
  });
});
