/**
 * Routing's terminal-state mapping.
 *
 * Same rule as scheduling — INFEASIBLE is a `warn`, not a `bad` — with one
 * addition that is specific to this model: too few vehicles for the service
 * minutes on offer is a one-field mistake on the trigger form, so the
 * infeasible copy has to carry the two numbers that say so.
 */

import { describe, expect, it } from "vitest";

import type { UiRunState } from "@/lib/envelope";

import type { ResolvedRoutes, RoutingShape } from "./routing";
import { DEFAULT_ROUTING_SHAPE } from "./routing";
import { describeRoutingState } from "./stateCopy";

const NO_ROUTES: ResolvedRoutes = {
  routes: [],
  stopCount: 0,
  rowCount: 0,
  previewCount: 0,
  truncated: false,
  empty: true,
  capacityMinutes: null,
};

const SOLVED: ResolvedRoutes = {
  ...NO_ROUTES,
  routes: [
    { index: 0, stops: [{ name: "a", x: 1, y: 1, order: 1 }], distance: 10, loadMinutes: 200 },
    { index: 1, stops: [{ name: "b", x: 2, y: 2, order: 1 }], distance: 14, loadMinutes: 240 },
  ],
  stopCount: 2,
  rowCount: 24,
  previewCount: 24,
  empty: false,
};

const OVERSUBSCRIBED: RoutingShape = {
  stopCount: 24,
  vehicles: 3,
  capacityMinutes: 480,
  requiredMinutes: 1610,
  source: "log",
};

function copy(
  state: UiRunState | null,
  overrides: Partial<Parameters<typeof describeRoutingState>[1]> = {},
) {
  return describeRoutingState(state, {
    shape: DEFAULT_ROUTING_SHAPE,
    routes: NO_ROUTES,
    solutionCount: null,
    detail: null,
    ...overrides,
  });
}

describe("describeRoutingState", () => {
  it("gives INFEASIBLE the warn tone and FAILED the bad tone", () => {
    expect(copy("INFEASIBLE").tone).toBe("warn");
    expect(copy("FAILED").tone).toBe("bad");
  });

  it("turns an infeasible fleet into arithmetic the user can act on", () => {
    const detail = copy("INFEASIBLE", { shape: OVERSUBSCRIBED }).detail;
    expect(detail).toMatch(/1,?440/); // 3 vehicles x 480 available
    expect(detail).toMatch(/1,?610/); // required
    expect(detail).toMatch(/170/); // the shortfall
    expect(detail).toMatch(/add a vehicle/i);
  });

  it("does not invent numbers when the instance line was never seen", () => {
    // `capacityShortfall` is null, not zero, so the copy falls back to prose.
    const detail = copy("INFEASIBLE").detail;
    expect(detail).toMatch(/not a crash/i);
    expect(detail).not.toMatch(/\bshort\b/);
  });

  it("covers every state, including no run at all", () => {
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

  it("counts tours and stops on a solved run", () => {
    const detail = copy("SUCCEEDED", { routes: SOLVED }).detail;
    expect(detail).toMatch(/2 tours over 2 stops/);
  });

  it("distinguishes a zero-row success from a routing", () => {
    expect(copy("SUCCEEDED").title).toMatch(/no stops written/i);
  });

  it("says a cancelled incumbent is a real routing, not a fragment", () => {
    // Every incumbent passed separation, so an interrupted run's routes are
    // still connected and capacity-feasible.
    expect(copy("CANCELLED", { routes: SOLVED }).detail).toMatch(/passed separation/i);
    expect(copy("CANCELLED").detail).toMatch(/before any connected routing/i);
  });

  it("frames a running solve as accepted candidates, not as raw solutions", () => {
    expect(copy("RUNNING").detail).toMatch(/no connected, capacity-feasible routing found yet/i);
    expect(copy("RUNNING", { solutionCount: 4 }).detail).toMatch(/4 candidate routings/);
  });
});
