/**
 * The lifecycle state machine, as words.
 *
 * Split out for the same reason as scheduling's: this is the part with real
 * failure modes. The one that matters most here is INFEASIBLE — "too few
 * vehicles for the service minutes on offer" is a one-field mistake on the
 * trigger form, so this file spends its longest string turning the two numbers
 * from the model's input log into an instruction.
 */

import type { UiRunState } from "@/lib/envelope";
import { formatCount, formatMetric } from "@/lib/format";
import type { StateCopy } from "../gurobi_shared/tones";
import { capacityShortfall, type ResolvedRoutes, type RoutingShape } from "./routing";

export function describeRoutingState(
  state: UiRunState | null,
  context: {
    shape: RoutingShape;
    routes: ResolvedRoutes;
    solutionCount: number | null;
    detail: string | null;
  },
): StateCopy {
  const { shape, routes, solutionCount, detail } = context;
  const suffix = detail ? ` (${detail})` : "";
  const shortfall = capacityShortfall(shape);

  switch (state) {
    case null:
      return {
        tone: "idle",
        hollow: true,
        title: "No run selected",
        detail: "Trigger a run to watch the vehicle routes solve.",
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
        title: "Searching and separating",
        detail:
          solutionCount === null
            ? "Branch and bound with lazy capacity cuts; no connected, capacity-feasible routing found yet."
            : `${formatCount(solutionCount)} candidate routings accepted so far — each one passed separation with no violated cut. The fan re-links on each.`,
      };
    case "SUCCEEDED":
      return routes.rowCount === 0
        ? {
            tone: "good",
            title: "Solve complete, no stops written",
            detail: `The solver finished${suffix} but the results table received zero rows.`,
          }
        : {
            tone: "good",
            title: "Routes solved",
            detail: `${formatCount(routes.routes.length)} tours over ${formatCount(routes.stopCount)} stops${suffix} — the fan below is the routing that was written.`,
          };
    case "INFEASIBLE":
      return {
        tone: "warn",
        title: "No feasible routing exists",
        detail:
          shortfall !== null && shortfall > 0
            ? `${formatCount(shape.vehicles)} vehicles x ${formatMetric(shape.capacityMinutes)} service-minutes = ${formatMetric(shape.vehicles * (shape.capacityMinutes ?? 0))} available, ${formatMetric(shape.requiredMinutes)} required — ${formatMetric(shortfall)} short. Add a vehicle or cut stops.`
            : "Proven infeasible: the fleet cannot cover these stops' service minutes. A correct answer about the inputs, not a crash.",
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
          routes.rowCount > 0
            ? `Stopped early; the incumbent routing at the moment of cancellation was still written (${formatCount(routes.rowCount)} stops). Every incumbent had already passed separation, so these are real routes, not fragments.`
            : "Stopped early, before any connected routing had been accepted.",
      };
  }
}
