/**
 * The lifecycle state machine, as words.
 *
 * Separate from the component because it is the part that can actually be
 * wrong — INFEASIBLE reading as a crash, a zero-row success reading as an
 * empty grid — and because a pure function of (state, facts) is testable
 * without rendering anything.
 */

import type { UiRunState } from "@/lib/envelope";
import { formatCount } from "@/lib/format";
import type { StateCopy } from "../gurobi_shared/tones";
import type { ResolvedSchedule } from "./schedule";

export function describeSchedulingState(
  state: UiRunState | null,
  context: {
    schedule: ResolvedSchedule;
    solutionCount: number | null;
    /** `detail` off the terminal status message — Gurobi's own word for the
     *  outcome ("optimal", "time limit reached", "infeasible"). */
    detail: string | null;
    clippedDemand: boolean;
  },
): StateCopy {
  const { schedule, solutionCount, detail, clippedDemand } = context;
  const suffix = detail ? ` (${detail})` : "";

  switch (state) {
    case null:
      return {
        tone: "idle",
        hollow: true,
        title: "No run selected",
        detail: "Trigger a run to watch the schedule solve.",
      };
    case "STARTING":
      return {
        tone: "accent",
        hollow: true,
        title: "Starting optimisation",
        detail: "Waiting for the job to pick the run up; a cold start takes tens of seconds.",
      };
    case "QUEUED":
      return {
        tone: "info",
        hollow: true,
        title: "Queued",
        detail: "Waiting for one of the five account-wide job slots.",
      };
    case "RUNNING":
      return {
        tone: "info",
        title: "Optimising schedule",
        detail:
          solutionCount === null
            ? "Branch and bound; no feasible schedule found yet."
            : `Branch and bound; the grid steps once per new incumbent (${formatCount(solutionCount)} so far).`,
      };
    case "SUCCEEDED":
      // "Succeeded with zero rows" is a real outcome and must not read as an
      // empty grid that nobody wrote down. `row_count` is populated exactly
      // so this case is distinguishable.
      return schedule.rowCount === 0
        ? {
            tone: "good",
            title: "Solve complete, no shifts written",
            detail: `The solver finished${suffix} but the results table received zero rows.`,
          }
        : {
            tone: "good",
            title: "Optimisation complete",
            detail: `${formatCount(schedule.rowCount)} assigned shifts${suffix} — the grid below is the schedule that was written.`,
          };
    case "INFEASIBLE":
      return {
        tone: "warn",
        title: "No feasible schedule exists",
        detail: clippedDemand
          ? "Proven infeasible: demand already had to be clipped to the workforce's capacity. This is a correct answer about the inputs, not a crash."
          : "Proven infeasible: coverage, one-shift-a-day, the shift cap and the rest rule cannot all hold at once. This is a correct answer about the inputs, not a crash.",
      };
    case "FAILED":
      return {
        tone: "bad",
        title: "Solve failed",
        detail: `The run did not reach a solver answer${suffix}. The log pane carries the reason.`,
      };
    case "CANCELLED":
      return {
        tone: "idle",
        title: "Cancelled",
        detail:
          schedule.rowCount > 0
            ? `Stopped early; the incumbent at the moment of cancellation was still written (${formatCount(schedule.rowCount)} shifts).`
            : "Stopped early, before any feasible schedule had been found.",
      };
  }
}
