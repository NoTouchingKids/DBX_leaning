/**
 * The lifecycle state machine, as words.
 *
 * Separate from the component because it is the part that can actually be
 * wrong — INFEASIBLE reading as a crash, a zero-row success reading as an
 * empty floor, "OPTIMAL" and "FEASIBLE" reading as the same outcome — and
 * because a pure function of (state, facts) is testable without rendering
 * anything.
 *
 * The one piece of copy here that no other model needs: an INFEASIBLE job shop
 * is a statement about the DEADLINE the user asked for. `instance.py` puts it
 * plainly — a pure job shop with an open horizon always has a schedule, since
 * the jobs can be run one after another — so `deadline_minutes` is the only
 * way this status is reachable, and the view has the deadline and the trivial
 * lower bound in hand to prove it.
 */

import type { UiRunState } from "@/lib/envelope";
import { formatCount } from "@/lib/format";
import type { InstanceShape, ResolvedSchedule } from "./schedule";
import type { StateCopy } from "./tone";

export interface JobshopStateContext {
  schedule: ResolvedSchedule;
  shape: InstanceShape;
  /** CP-SAT's exact count of improving solutions, or null if none reported. */
  solutionsFound: number | null;
  /** Observed improvement EVENTS — what the animation is paced on. */
  improvements: number;
  /** `detail` off the terminal status message. For this model that is the
   *  string `run()` returned: "optimal: makespan 412 min", "feasible: makespan
   *  430 min, gap 4.2%", "no schedule found (UNKNOWN)". */
  detail: string | null;
  /** CP-SAT's own status name, from the final progress sample or the result
   *  rows. NOT a `RunStatus` — OPTIMAL / FEASIBLE / INFEASIBLE /
   *  MODEL_INVALID / UNKNOWN. */
  solverStatus: string | null;
}

export function describeJobshopState(
  state: UiRunState | null,
  context: JobshopStateContext,
): StateCopy {
  const { schedule, shape, solutionsFound, improvements, detail, solverStatus } = context;
  const suffix = detail ? ` (${detail})` : "";

  switch (state) {
    case null:
      return {
        tone: "idle",
        hollow: true,
        title: "No run selected",
        detail: "Trigger a run to watch the shop floor fill up.",
      };
    case "STARTING":
      return {
        tone: "accent",
        hollow: true,
        title: "Starting solve",
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
      if (solutionsFound === null || solutionsFound === 0) {
        return {
          tone: "info",
          title: "Searching for a first schedule",
          detail:
            "CP-SAT reports only when it improves, so silence here is the search working, not a stalled stream. Every bar below is decorative until it does.",
        };
      }
      return {
        tone: "info",
        title: "Improving the schedule",
        detail:
          `${formatCount(solutionsFound)} improving solution${solutionsFound === 1 ? "" : "s"} so far; ` +
          `the floor redraws once per improvement (${formatCount(improvements)} observed). ` +
          "CP-SAT improves a handful of times per run, so a still floor is a search that is not improving.",
      };
    case "SUCCEEDED":
      // "Succeeded with zero rows" is a real outcome and must not read as an
      // empty floor nobody wrote down. `row_count` exists so the two are
      // distinguishable, and this model reaches it whenever the search stopped
      // before any feasible schedule ("no schedule found (UNKNOWN)").
      if (schedule.rowCount === 0) {
        return {
          tone: "good",
          title: "Search complete, no schedule written",
          detail: `The solver finished${suffix} but the results table received zero rows — no feasible schedule was found before it stopped.`,
        };
      }
      return {
        tone: "good",
        title: solverStatus === "OPTIMAL" ? "Proven optimal" : "Best schedule found",
        detail:
          solverStatus === "OPTIMAL"
            ? `${formatCount(schedule.rowCount)} scheduled operations${suffix} — CP-SAT closed the gap, so no shorter makespan exists.`
            : `${formatCount(schedule.rowCount)} scheduled operations${suffix} — the best found before the search stopped, not a proof that none is shorter.`,
      };
    case "INFEASIBLE":
      return {
        tone: "warn",
        title: "No schedule meets the deadline",
        detail: deadlineExplanation(shape),
      };
    case "FAILED":
      return {
        tone: "bad",
        title: "Solve failed",
        detail:
          solverStatus === "MODEL_INVALID"
            ? "CP-SAT rejected the model this run built. That is a defect in the model code, not an outcome about the bakery — the log pane carries the reason."
            : `The run did not reach a solver answer${suffix}. The log pane carries the reason.`,
      };
    case "CANCELLED":
      return {
        tone: "idle",
        title: "Cancelled",
        detail:
          schedule.rowCount > 0
            ? `Stopped early; the best schedule at the moment of cancellation was still written (${formatCount(schedule.rowCount)} operations).`
            : "Stopped early, before any feasible schedule had been found.",
      };
  }
}

/**
 * Why an infeasible job shop is a statement about the input.
 *
 * With both numbers in hand this is a proof rather than a hunch: the trivial
 * lower bound is the longest job or the busiest machine, whichever is larger,
 * and no schedule can beat it. A deadline below it is impossible by
 * arithmetic, and saying so is more useful than "the solver said no".
 */
function deadlineExplanation(shape: InstanceShape): string {
  const bound = shape.makespanLowerBound;
  const deadline = shape.deadlineMinutes;
  const tail =
    "A job shop with an open horizon always has a schedule — the jobs can simply be run one after another — so the deadline is the only way this model can be infeasible. This is a correct answer about your input, not a solver failure.";

  if (deadline !== null && bound !== null) {
    return `The ${formatCount(deadline)} min deadline is below this instance's floor of ${formatCount(bound)} min (the longest job, or the busiest machine's total load). ${tail}`;
  }
  if (deadline !== null) {
    return `CP-SAT proved that nothing fits inside the ${formatCount(deadline)} min deadline. ${tail}`;
  }
  return `CP-SAT proved this instance has no schedule. ${tail}`;
}
