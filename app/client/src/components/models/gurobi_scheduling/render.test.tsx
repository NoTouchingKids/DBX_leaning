/**
 * Every state of both views, rendered once.
 *
 * This asserts nothing about markup — it asserts that no combination of
 * lifecycle state and data reaches a crash. Both signatures are state machines
 * over eight states (seven statuses plus "no run selected") crossed with "has
 * result rows" and "has none", and the cheap failures in that grid are real:
 * indexing an empty staff list, projecting a route with no stops, dividing by
 * a vehicle count of zero. `noUncheckedIndexedAccess` catches some of it; the
 * rest is only reachable at runtime.
 */

import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import type { RunSnapshot } from "@/transport/runStore";
import type { UiRunState } from "@/lib/envelope";
import scheduling from "./index";
import routing from "../gurobi_routing/index";

const base: RunSnapshot = {
  run_id: "run-00000000abcd",
  logs: [
    { type: "log", run_id: "r", seq: 1, ts: 1, message: "building: 20 staff x 14 days x 3 shifts", level: "INFO", source: "model", phase: "build", client_visible: true },
    { type: "log", run_id: "r", seq: 2, ts: 2, message: "routing 24 stops with 3 vehicles, 480 service-minutes each (1610 required), at 1.35 per unit distance", level: "INFO", source: "model", phase: "input", client_visible: true },
  ],
  progress: [
    { type: "progress", run_id: "r", seq: 3, ts: 3, elapsed_seconds: 2, percent_complete: null, primary_metric: null, primary_metric_label: "mip_gap", payload: { best_bound: null, incumbent: null, nodes_explored: 0, nodes_remaining: 0, solution_count: 0 } },
    { type: "progress", run_id: "r", seq: 4, ts: 4, elapsed_seconds: 4, percent_complete: null, primary_metric: 0.12, primary_metric_label: "mip_gap", payload: { best_bound: 400, incumbent: 460, nodes_explored: 120, nodes_remaining: 40, solution_count: 2 } },
  ],
  statuses: [],
  results: [],
  latestProgress: null,
  status: "RUNNING",
  terminal: false,
  lastSeq: 4,
  connection: "open",
  consecutiveFailures: 0,
  gaps: [],
  hydrated: true,
  droppedLogs: 0,
  droppedProgress: 0,
};

const withResults: RunSnapshot = {
  ...base,
  terminal: true,
  status: "SUCCEEDED",
  results: [
    { type: "result", run_id: "r", seq: 10, ts: 10, chunk_index: 0, final: true, row_count: 3, fetch_hint: {}, preview: [
      { staff: "staff-00", day: 0, shift: "morning", preferred: true },
      { staff: "staff-01", day: 1, shift: "night", preferred: false },
      // Deliberately both models' row shapes in one preview: each resolver
      // must take its own rows and skip the other's rather than throwing.
      { route: 0, visit_order: 1, stop: "s1", x: 3, y: 4, service_minutes: 20, route_distance: 9, route_load_minutes: 200, vehicle_capacity_minutes: 480 },
    ] },
  ],
};

const states: (UiRunState | null)[] = [null, "STARTING", "QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", "INFEASIBLE"];

describe("both Gurobi views", () => {
  it("render in every lifecycle state, with and without result rows", () => {
    for (const view of [scheduling, routing]) {
      for (const snapshot of [base, withResults]) {
        for (const state of states) {
          expect(() => render(<view.Signature state={state} snapshot={snapshot} />)).not.toThrow();
          for (const chart of view.charts) {
            expect(() => render(<chart.Chart state={state} snapshot={snapshot} />)).not.toThrow();
          }
        }
      }
    }
  });
});
