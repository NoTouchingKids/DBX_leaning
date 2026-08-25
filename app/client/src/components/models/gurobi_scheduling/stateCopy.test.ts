/**
 * The terminal-state mapping.
 *
 * The one that matters: INFEASIBLE must not be dressed as FAILED. An
 * infeasible MILP is the solver correctly answering that the request is
 * impossible — a `warn`, with an explanation of which inputs collide. A
 * `FAILED` run is the one where nobody knows anything. Colouring them the same
 * teaches a user to read a correct answer as an outage.
 */

import { describe, expect, it } from "vitest";

import type { UiRunState } from "@/lib/envelope";

import type { ResolvedSchedule } from "./schedule";
import { describeSchedulingState } from "./stateCopy";

const NO_SCHEDULE: ResolvedSchedule = {
  assignments: [],
  staff: [],
  days: [],
  shifts: [],
  rowCount: 0,
  previewCount: 0,
  truncated: false,
  empty: true,
};

const SOLVED: ResolvedSchedule = {
  ...NO_SCHEDULE,
  rowCount: 236,
  previewCount: 236,
  empty: false,
};

function copy(state: UiRunState | null, overrides: Partial<Parameters<typeof describeSchedulingState>[1]> = {}) {
  return describeSchedulingState(state, {
    schedule: NO_SCHEDULE,
    solutionCount: null,
    detail: null,
    clippedDemand: false,
    ...overrides,
  });
}

describe("describeSchedulingState", () => {
  it("gives INFEASIBLE the warn tone and FAILED the bad tone", () => {
    expect(copy("INFEASIBLE").tone).toBe("warn");
    expect(copy("FAILED").tone).toBe("bad");
  });

  it("says out loud that an infeasible model is an answer, not a crash", () => {
    expect(copy("INFEASIBLE").detail).toMatch(/not a crash/i);
    expect(copy("INFEASIBLE").title).not.toMatch(/fail/i);
  });

  it("names the clipped demand curve when that is what made it infeasible", () => {
    expect(copy("INFEASIBLE", { clippedDemand: true }).detail).toMatch(/clipped/i);
  });

  it("covers every state the page can be in, including no run at all", () => {
    const states: (UiRunState | null)[] = [
      null,
      "STARTING",
      "QUEUED",
      "RUNNING",
      "SUCCEEDED",
      "FAILED",
      "CANCELLED",
      "INFEASIBLE",
    ];
    for (const state of states) {
      const result = copy(state);
      expect(result.title.length).toBeGreaterThan(0);
      expect(result.detail.length).toBeGreaterThan(0);
    }
  });

  it("hollows the dot for the states where nothing has been solved yet", () => {
    expect(copy("QUEUED").hollow).toBe(true);
    expect(copy("STARTING").hollow).toBe(true);
    expect(copy("RUNNING").hollow).toBeUndefined();
  });

  it("distinguishes a zero-row success from a schedule", () => {
    // `row_count` exists precisely so "succeeded, wrote nothing" is not
    // rendered as an empty grid nobody notices.
    expect(copy("SUCCEEDED").title).toMatch(/no shifts written/i);
    expect(copy("SUCCEEDED", { schedule: SOLVED }).title).toMatch(/complete/i);
    expect(copy("SUCCEEDED", { schedule: SOLVED }).detail).toMatch(/236/);
  });

  it("carries Gurobi's own word for the outcome when there is one", () => {
    // "time limit reached" and "optimal" are both SUCCEEDED and are not the
    // same run.
    expect(copy("SUCCEEDED", { schedule: SOLVED, detail: "time limit reached" }).detail).toMatch(
      /time limit reached/,
    );
  });

  it("tells a cancelled run whether its incumbent survived", () => {
    // `results()` is never gated on OPTIMAL, so a cancelled run usually keeps
    // a schedule — and that is the difference between a wasted run and a
    // usable one.
    expect(copy("CANCELLED").detail).toMatch(/before any feasible schedule/i);
    expect(copy("CANCELLED", { schedule: SOLVED }).detail).toMatch(/still written/i);
  });

  it("says the search has found nothing yet rather than showing a bare zero", () => {
    expect(copy("RUNNING").detail).toMatch(/no feasible schedule found yet/i);
    expect(copy("RUNNING", { solutionCount: 5 }).detail).toMatch(/5/);
  });
});
