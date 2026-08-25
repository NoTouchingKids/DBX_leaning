/**
 * The derivations, not the markup.
 *
 * `fixtures.ts` has no script for this model (`hasScript("panel_fit")` is
 * false), so these build their own payloads straight from
 * `job/models/panel_fit/model.py::_progress` and `_flush`. That is the right
 * source anyway: `@/lib/models` is hand-derived from the same place and can
 * drift, so a test written against the model rather than against the interface
 * is the one that would notice.
 */

import { describe, expect, it } from "vitest";

import type { ProgressMessage, ResultMessage } from "@/lib/envelope";
import { PANEL_FIT_FAILURE_REASONS } from "@/lib/models";

import {
  accumulateGroups,
  arrivalState,
  buildGroupPoints,
  buildLattice,
  failureBreakdown,
  failuresOf,
  formatRSquared,
  formatShare,
  LATTICE_MAX_CELLS,
  metricHigherIsBetter,
  NO_COUNTS,
  NO_DURABLE,
  NO_LATTICE,
  readCounts,
  readProvenance,
} from "./panelModel";

/* ------------------------------------------------------------------ *
 * Builders, shaped exactly like the model's own emissions
 * ------------------------------------------------------------------ */

const SYNTHETIC = {
  data_source: "synthetic:owid-panel",
  data_synthetic: true,
  data_rows: 1_284,
  data_fallback_reason: "main.dbx_leaning.owid_country_year does not exist",
};

function progress(payload: Record<string, unknown>, metric: number | null = 0.91): ProgressMessage {
  const done = typeof payload["groups_done"] === "number" ? payload["groups_done"] : 1;
  return {
    type: "progress",
    run_id: "run-000000000001",
    seq: 100 + done,
    ts: 1_700_000_000_000 + done,
    elapsed_seconds: done * 0.02,
    percent_complete: null,
    primary_metric: metric,
    primary_metric_label: "median_r_squared",
    payload: {
      groups_total: 48,
      metric_higher_is_better: true,
      degree: 1,
      chunks_emitted: 0,
      ...SYNTHETIC,
      ...payload,
    },
  };
}

/** A run of `total` groups where `failed` of them fail, emitted one message
 *  per group the way the model does at `progress_every: 1`. */
function runOf(
  total: number,
  failed: number,
  reason = "too_few_observations",
): ProgressMessage[] {
  const out: ProgressMessage[] = [];
  let fittedSoFar = 0;
  let failedSoFar = 0;
  const counts: Record<string, number> = {};

  for (let done = 1; done <= total; done += 1) {
    // Failures first, so a partial run in these tests is not accidentally
    // all-healthy for its first half.
    const fails = done <= failed;
    if (fails) {
      failedSoFar += 1;
      counts[reason] = (counts[reason] ?? 0) + 1;
    } else {
      fittedSoFar += 1;
    }
    out.push(
      progress({
        groups_done: done,
        groups_total: total,
        groups_fitted: fittedSoFar,
        groups_failed: failedSoFar,
        failure_counts: { ...counts },
        group_key: `country-${done}`,
        group_label: `C${done}`,
        group_status: fails ? "failed" : "fitted",
        group_failure_reason: fails ? reason : null,
        group_r_squared: fails ? null : 0.8 + (done % 10) / 100,
        n_observations: fails ? 2 : 40,
        rows_seen: fails ? 21 : 42,
      }),
    );
  }
  return out;
}

function chunk(index: number, groups: number, over: Partial<ResultMessage> = {}): ResultMessage {
  const preview = Array.from({ length: groups }, (_, i) => ({
    group_key: `country-${index * 12 + i}`,
    group_label: `C${index * 12 + i}`,
    status: i % 4 === 0 ? "failed" : "fitted",
    failure_reason: i % 4 === 0 ? "too_few_observations" : null,
    r_squared: i % 4 === 0 ? null : 0.9,
    n_observations: i % 4 === 0 ? 2 : 40,
    first_period: 1960,
    last_period: 2023,
  }));
  return {
    type: "result",
    run_id: "run-000000000001",
    seq: 500 + index,
    ts: 1_700_000_000_000,
    preview,
    row_count: preview.length,
    fetch_hint: { table: "main.dbx_leaning.results_panel_fit", key: "run_id" },
    chunk_index: index,
    final: false,
    ...over,
  };
}

/* ------------------------------------------------------------------ *
 * The invariant
 * ------------------------------------------------------------------ */

describe("readCounts", () => {
  it("knows nothing from nothing", () => {
    expect(readCounts(null)).toEqual(NO_COUNTS);
    expect(readCounts(progress({}))).toMatchObject({ done: null, fitted: null, failed: null });
  });

  it("holds groups_fitted + groups_failed === groups_done across a whole run", () => {
    // The invariant this view is built on. If it ever stops holding, the
    // outcome bar is showing a lie — which is why `consistent` is computed
    // rather than assumed.
    for (const message of runOf(48, 9)) {
      const counts = readCounts(message);
      expect(counts.consistent, `at groups_done ${counts.done}`).toBe(true);
      expect((counts.fitted ?? 0) + (counts.failed ?? 0)).toBe(counts.done);
    }
  });

  it("reports a payload that contradicts itself instead of picking a number", () => {
    const counts = readCounts(
      progress({ groups_done: 10, groups_fitted: 6, groups_failed: 2, groups_total: 48 }),
    );
    expect(counts.consistent).toBe(false);
    // The three numbers all survive: the component names them rather than
    // reconciling them.
    expect([counts.done, counts.fitted, counts.failed]).toEqual([10, 6, 2]);
  });

  it("leaves consistency null when the run has not reported all three", () => {
    expect(readCounts(progress({ groups_done: 4 })).consistent).toBeNull();
  });

  it("counts remaining against the total, which is known before the first fit", () => {
    const counts = readCounts(
      progress({ groups_done: 10, groups_fitted: 8, groups_failed: 2, groups_total: 48 }),
    );
    expect(counts.remaining).toBe(38);
  });
});

/* ------------------------------------------------------------------ *
 * Where "normal" stops
 * ------------------------------------------------------------------ */

describe("failure tone", () => {
  it("is silent when no group has failed", () => {
    const counts = readCounts(
      progress({ groups_done: 30, groups_fitted: 30, groups_failed: 0, groups_total: 48 }),
    );
    expect(counts.tone).toBe("none");
    expect(counts.allFailed).toBe(false);
    expect(counts.failedShare).toBe(0);
  });

  it("treats a minority of failures as routine — the default panel's own shape", () => {
    // The synthetic panel is built with 9 unfittable groups out of 48 at
    // degree 1. That is a correct, healthy run and must not be dressed as
    // anything else.
    const counts = readCounts(runOf(48, 9).at(-1) ?? null);
    expect(counts.tone).toBe("routine");
    expect(formatShare(counts.failedShare)).toBe("19%");
  });

  it("crosses to notable at exactly half the completed groups", () => {
    const under = readCounts(
      progress({ groups_done: 10, groups_fitted: 6, groups_failed: 4, groups_total: 48 }),
    );
    const at = readCounts(
      progress({ groups_done: 10, groups_fitted: 5, groups_failed: 5, groups_total: 48 }),
    );
    expect(under.tone).toBe("routine");
    expect(at.tone).toBe("notable");
  });

  it("has no tone that could be styled as an error", () => {
    // `FailureTone` deliberately has no `bad` member. This is the runtime
    // half of that guarantee: whatever the numbers, the tone is one of three
    // calm values.
    const tones = new Set(
      [runOf(6, 0), runOf(6, 1), runOf(6, 3), runOf(6, 6)].map(
        (messages) => readCounts(messages.at(-1) ?? null).tone,
      ),
    );
    expect([...tones].sort()).toEqual(["none", "notable", "routine"]);
  });

  it("flags an all-failed run — the state the model itself calls INFEASIBLE", () => {
    const counts = readCounts(runOf(12, 12).at(-1) ?? null);
    expect(counts.allFailed).toBe(true);
    expect(counts.fitted).toBe(0);
    expect(counts.tone).toBe("notable");
  });

  it("never rounds a real failure rate down to nothing", () => {
    const counts = readCounts(
      progress({ groups_done: 500, groups_fitted: 499, groups_failed: 1, groups_total: 500 }),
    );
    expect(formatShare(counts.failedShare)).toBe("<1%");
  });
});

/* ------------------------------------------------------------------ *
 * The lattice
 * ------------------------------------------------------------------ */

describe("buildLattice", () => {
  it("knows nothing from nothing", () => {
    expect(buildLattice(NO_COUNTS)).toEqual(NO_LATTICE);
  });

  it("gives one cell per group while the panel fits the grid", () => {
    const lattice = buildLattice(readCounts(runOf(48, 9).at(-1) ?? null));
    expect(lattice.oneCellPerGroup).toBe(true);
    expect(lattice.cells).toHaveLength(48);
    expect(lattice.cells.filter((c) => c === "fitted")).toHaveLength(39);
    expect(lattice.cells.filter((c) => c === "failed")).toHaveLength(9);
    expect(lattice.cells.filter((c) => c === "pending")).toHaveLength(0);
  });

  it("compresses a larger panel and says so", () => {
    const lattice = buildLattice(
      readCounts(
        progress({ groups_done: 180, groups_fitted: 168, groups_failed: 12, groups_total: 180 }),
      ),
    );
    expect(lattice.oneCellPerGroup).toBe(false);
    expect(lattice.cells).toHaveLength(LATTICE_MAX_CELLS);
    expect(lattice.groupsPerCell).toBeCloseTo(180 / LATTICE_MAX_CELLS);
  });

  it("never rounds a nonzero failure count down to zero cells", () => {
    // One failure in five hundred groups is under a fifth of a cell. Dropping
    // it would delete the one thing this view exists to show.
    const lattice = buildLattice(
      readCounts(
        progress({ groups_done: 500, groups_fitted: 499, groups_failed: 1, groups_total: 500 }),
      ),
    );
    expect(lattice.cells.filter((c) => c === "failed")).toHaveLength(1);
    expect(lattice.cells).toHaveLength(LATTICE_MAX_CELLS);
  });

  it("never overflows the grid, however the rounding falls", () => {
    for (let failed = 0; failed <= 500; failed += 7) {
      const lattice = buildLattice(
        readCounts(
          progress({
            groups_done: 500,
            groups_fitted: 500 - failed,
            groups_failed: failed,
            groups_total: 500,
          }),
        ),
      );
      expect(lattice.cells, `failed ${failed}`).toHaveLength(LATTICE_MAX_CELLS);
    }
  });

  it("puts the frontier on the first pending cell, which is groups_done", () => {
    const lattice = buildLattice(
      readCounts(
        progress({ groups_done: 20, groups_fitted: 16, groups_failed: 4, groups_total: 48 }),
      ),
    );
    // Falls out of the invariant: the fitted and failed blocks together ARE
    // the completed groups.
    expect(lattice.frontier).toBe(20);
    expect(lattice.cells[20]).toBe("pending");
  });

  it("has no frontier once every group is done", () => {
    expect(buildLattice(readCounts(runOf(48, 9).at(-1) ?? null)).frontier).toBeNull();
  });

  it("shows an all-failed run as an entirely failed grid", () => {
    const lattice = buildLattice(readCounts(runOf(12, 12).at(-1) ?? null));
    expect(new Set(lattice.cells)).toEqual(new Set(["failed"]));
  });

  it("shows a zero-failure run with no failed cell at all", () => {
    const lattice = buildLattice(readCounts(runOf(12, 0).at(-1) ?? null));
    expect(lattice.cells.filter((c) => c === "failed")).toHaveLength(0);
  });
});

/* ------------------------------------------------------------------ *
 * Reasons
 * ------------------------------------------------------------------ */

describe("failureBreakdown", () => {
  it("declares all four reasons, including the zeroes", () => {
    const breakdown = failureBreakdown(
      progress({ failure_counts: { too_few_observations: 3 } }),
    );
    expect(breakdown.reasons.map((r) => r.reason)).toEqual([...PANEL_FIT_FAILURE_REASONS]);
    expect(breakdown.total).toBe(3);
    expect(breakdown.present.map((r) => r.reason)).toEqual(["too_few_observations"]);
  });

  it("gives every declared reason its own label and meaning", () => {
    // "Failed how" is the question this card answers, so two reasons that read
    // the same are the same as not answering it.
    const breakdown = failureBreakdown(
      progress({
        failure_counts: {
          too_few_observations: 4,
          zero_predictor_variance: 2,
          singular_design: 3,
          non_finite_result: 1,
        },
      }),
    );
    expect(breakdown.total).toBe(10);
    expect(new Set(breakdown.reasons.map((r) => r.label)).size).toBe(4);
    expect(new Set(breakdown.reasons.map((r) => r.meaning)).size).toBe(4);
    for (const reason of breakdown.reasons) {
      expect(reason.known).toBe(true);
      expect(reason.meaning.length).toBeGreaterThan(40);
    }
  });

  it("keeps the declared order so bars do not reshuffle mid-run", () => {
    const first = failureBreakdown(progress({ failure_counts: { singular_design: 9 } }));
    const later = failureBreakdown(
      progress({ failure_counts: { singular_design: 9, too_few_observations: 30 } }),
    );
    expect(first.reasons.map((r) => r.reason)).toEqual(later.reasons.map((r) => r.reason));
  });

  it("names the dominant reason, which is the actionable half", () => {
    const breakdown = failureBreakdown(
      progress({ failure_counts: { too_few_observations: 2, singular_design: 31 } }),
    );
    // singular_design dominating says the DEGREE is wrong for this predictor;
    // too_few_observations dominating says the panel is thin. Different
    // answers, so the view has to be able to tell them apart.
    expect(breakdown.dominant?.reason).toBe("singular_design");
    expect(breakdown.peak).toBe(31);
  });

  it("surfaces a reason the model should not be able to emit", () => {
    // FAILURE_REASONS is closed, so a fifth value is a bug in the model.
    // Dropping it would hide a failure that really happened.
    const breakdown = failureBreakdown(progress({ failure_counts: { made_up_reason: 2 } }));
    const extra = breakdown.reasons.find((r) => r.reason === "made_up_reason");
    expect(extra?.known).toBe(false);
    expect(extra?.count).toBe(2);
    expect(breakdown.total).toBe(2);
  });

  it("is empty, not broken, before anything has failed", () => {
    expect(failureBreakdown(null).total).toBe(0);
    expect(failureBreakdown(progress({ failure_counts: {} })).dominant).toBeNull();
  });
});

/* ------------------------------------------------------------------ *
 * Per-group points
 * ------------------------------------------------------------------ */

describe("buildGroupPoints", () => {
  it("carries the usable-of-seen pair, which is the first question about a failure", () => {
    const failures = failuresOf(buildGroupPoints(runOf(20, 5)));
    expect(failures).toHaveLength(5);
    for (const failure of failures) {
      expect(failure.nObservations).toBe(2);
      expect(failure.rowsSeen).toBe(21);
      expect(failure.rSquared).toBeNull();
    }
  });

  it("deduplicates on groups_done so one group is one point", () => {
    const messages = runOf(5, 0);
    const first = messages[0];
    expect(first).toBeDefined();
    const points = buildGroupPoints([...messages, { ...(first as ProgressMessage), seq: 999 }]);
    expect(points.map((p) => p.done)).toEqual([1, 2, 3, 4, 5]);
  });

  it("sorts by position however the messages arrived", () => {
    const points = buildGroupPoints([...runOf(6, 0)].reverse());
    expect(points.map((p) => p.done)).toEqual([1, 2, 3, 4, 5, 6]);
  });

  it("ignores a group_status the model does not declare", () => {
    const point = buildGroupPoints([progress({ groups_done: 1, group_status: "partial" })])[0];
    expect(point?.status).toBeNull();
  });

  it("reads the metric direction from the run rather than assuming it", () => {
    // Higher IS better here, unlike forecasting — but the point of reading it
    // is that a direction copied between model directories is a bug nothing
    // catches.
    expect(metricHigherIsBetter(progress({}))).toBe(true);
    expect(metricHigherIsBetter(progress({ metric_higher_is_better: false }))).toBe(false);
    expect(metricHigherIsBetter(null)).toBe(true);
  });

  it("keeps a null median, which is legal until something has been fitted", () => {
    const point = buildGroupPoints([progress({ groups_done: 1 }, null)])[0];
    expect(point?.median).toBeNull();
  });
});

/* ------------------------------------------------------------------ *
 * Provenance
 * ------------------------------------------------------------------ */

describe("readProvenance", () => {
  it("reads the synthetic fallback the default run actually takes", () => {
    // `main.dbx_leaning.owid_country_year` has never been created, so this is
    // the DEFAULT path, not an edge case.
    const provenance = readProvenance(progress({ groups_done: 1 }));
    expect(provenance.synthetic).toBe(true);
    expect(provenance.source).toBe("synthetic:owid-panel");
    expect(provenance.fallbackReason).toContain("owid_country_year");
  });

  it("reads a real read as real, with no fallback reason", () => {
    const provenance = readProvenance(
      progress({
        data_source: "main.dbx_leaning.owid_country_year",
        data_synthetic: false,
        data_fallback_reason: null,
      }),
    );
    expect(provenance.synthetic).toBe(false);
    expect(provenance.fallbackReason).toBeNull();
  });

  it("does not claim data is real when the run has not said", () => {
    // "We do not know whether this is real" and "this is real" are different
    // claims, and only one of them justifies reading the chart.
    expect(readProvenance(null).synthetic).toBeNull();
    expect(readProvenance(progress({ data_synthetic: "yes" })).synthetic).toBeNull();
  });
});

/* ------------------------------------------------------------------ *
 * Chunks
 * ------------------------------------------------------------------ */

describe("accumulateGroups", () => {
  it("knows nothing from nothing", () => {
    expect(accumulateGroups([])).toEqual(NO_DURABLE);
  });

  it("appends chunks rather than replacing them", () => {
    const view = accumulateGroups([chunk(0, 12), chunk(1, 12), chunk(2, 4, { final: true })]);
    expect(view.rowsWritten).toBe(28);
    expect(view.rows).toHaveLength(28);
    expect(view.complete).toBe(true);
  });

  it("counts failed groups in the durable record — they are recorded, not dropped", () => {
    // The model's central promise. A failed group is a row with a reason and
    // null coefficients, so the written count includes it.
    const view = accumulateGroups([chunk(0, 12, { final: true })]);
    expect(view.failedRows).toBe(3);
    expect(view.fittedRows).toBe(9);
    expect(view.fittedRows + view.failedRows).toBe(view.rowsWritten);
  });

  it("deduplicates a re-emitted chunk on chunk_index, not seq", () => {
    // The store already drops a repeated seq; this covers a job retry
    // re-emitting the same chunk under a new one, which would otherwise
    // double every group in it.
    const view = accumulateGroups([chunk(0, 12), { ...chunk(0, 12), seq: 9_999 }]);
    expect(view.chunks).toHaveLength(1);
    expect(view.rowsWritten).toBe(12);
  });

  it("is complete if any copy of a chunk carried final", () => {
    const view = accumulateGroups([chunk(0, 12), { ...chunk(0, 12), seq: 9_999, final: true }]);
    expect(view.complete).toBe(true);
  });

  it("reports a hole rather than a shortened tally", () => {
    const view = accumulateGroups([chunk(0, 12), chunk(2, 12)]);
    expect(view.missing).toEqual([1]);
    expect(view.rowsWritten).toBe(24);
  });

  it("counts an empty final chunk — the model emits one on every path", () => {
    // A panel with no groups at all still gets exactly one `final: true`, so a
    // client's "results are complete" condition is reachable.
    const view = accumulateGroups([chunk(0, 0, { final: true })]);
    expect(view.complete).toBe(true);
    expect(view.rowsWritten).toBe(0);
    expect(view.chunks).toHaveLength(1);
  });
});

describe("arrivalState", () => {
  it("separates a run that stopped from one still arriving", () => {
    const partial = accumulateGroups([chunk(0, 12)]);
    expect(arrivalState(partial, false)).toBe("arriving");
    // A cancelled run keeps its chunks but will never send a final one, so
    // "still arriving" would be waiting for something that is not coming.
    expect(arrivalState(partial, true)).toBe("stopped");
  });

  it("is complete once a final chunk has been seen, terminal or not", () => {
    const done = accumulateGroups([chunk(0, 12, { final: true })]);
    expect(arrivalState(done, false)).toBe("complete");
    expect(arrivalState(done, true)).toBe("complete");
  });

  it("says nothing has arrived before the first chunk", () => {
    expect(arrivalState(NO_DURABLE, false)).toBe("none");
  });
});

/* ------------------------------------------------------------------ *
 * Formatting
 * ------------------------------------------------------------------ */

describe("formatRSquared", () => {
  it("keeps a negative R-squared, which is a real answer", () => {
    // A fit worse than the group's own mean. Clamping it at zero would hide
    // the worst fits in the panel.
    expect(formatRSquared(-0.42)).toBe("-0.420");
  });

  it("returns null for the values that are genuinely absent", () => {
    expect(formatRSquared(null)).toBeNull();
    expect(formatRSquared(undefined)).toBeNull();
    expect(formatRSquared(Number.NaN)).toBeNull();
  });
});
