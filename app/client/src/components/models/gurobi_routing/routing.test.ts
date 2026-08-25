/**
 * The fan's inputs: the instance read out of the log stream, the real tours
 * read out of the result rows, the decorative layer's determinism, and the
 * projection that has to keep the depot inside its own picture.
 */

import { describe, expect, it } from "vitest";

import type { LogMessage, ResultMessage } from "@/lib/envelope";

import {
  capacityShortfall,
  DEFAULT_ROUTING_SHAPE,
  decorativeRoutes,
  decorativeStops,
  parseRoutingShape,
  projection,
  resolveRoutes,
  tourPath,
} from "./routing";

function log(seq: number, message: string): LogMessage {
  return {
    type: "log",
    run_id: "run-00000000abcd",
    seq,
    ts: 1_700_000_000_000 + seq,
    message,
    level: "INFO",
    source: "model",
    phase: "input",
    client_visible: true,
  };
}

const INSTANCE_LINE =
  "routing 24 stops with 3 vehicles, 480 service-minutes each (1610 required), at 1.35 per unit distance";

function result(
  preview: Array<Record<string, unknown>>,
  rowCount = preview.length,
): ResultMessage {
  return {
    type: "result",
    run_id: "run-00000000abcd",
    seq: 900,
    ts: 1_700_000_000_900,
    preview,
    row_count: rowCount,
    fetch_hint: { table: "main.default.results_gurobi_routing", key: "run_id" },
    chunk_index: 0,
    final: true,
  };
}

function stop(route: number, order: number, x: number, y: number, extra = {}) {
  return {
    route,
    visit_order: order,
    stop: `stop-${route}-${order}`,
    previous_stop: "depot",
    x,
    y,
    service_minutes: 22,
    route_distance: 12.5,
    route_load_minutes: 300,
    vehicle_capacity_minutes: 480,
    ...extra,
  };
}

describe("parseRoutingShape", () => {
  it("reads stops, vehicles and both capacity numbers from the input log", () => {
    expect(parseRoutingShape([log(2, INSTANCE_LINE)])).toEqual({
      stopCount: 24,
      vehicles: 3,
      capacityMinutes: 480,
      requiredMinutes: 1610,
      source: "log",
    });
  });

  it("falls back to a labelled default when the line has not arrived", () => {
    expect(parseRoutingShape([])).toEqual(DEFAULT_ROUTING_SHAPE);
    expect(parseRoutingShape([]).capacityMinutes).toBeNull();
  });

  it("takes the most recent instance line", () => {
    const shape = parseRoutingShape([
      log(1, INSTANCE_LINE),
      log(2, "routing 8 stops with 2 vehicles, 300 service-minutes each (410 required), at 1.0 per unit distance"),
    ]);
    expect(shape.stopCount).toBe(8);
    expect(shape.vehicles).toBe(2);
  });
});

describe("capacityShortfall", () => {
  it("computes the number that explains an INFEASIBLE routing run", () => {
    // 3 x 480 = 1440 available against 1610 required: 170 short, and the fix
    // is one field on the form.
    expect(capacityShortfall(parseRoutingShape([log(1, INSTANCE_LINE)]))).toBe(170);
  });

  it("is negative when the fleet has slack", () => {
    const shape = parseRoutingShape([
      log(1, "routing 8 stops with 4 vehicles, 480 service-minutes each (400 required), at 1.0 per unit distance"),
    ]);
    expect(capacityShortfall(shape)).toBe(-1520);
  });

  it("is null — not zero — when the log line has not been seen", () => {
    // "Unknown" and "fine" are different answers.
    expect(capacityShortfall(DEFAULT_ROUTING_SHAPE)).toBeNull();
  });
});

describe("resolveRoutes", () => {
  it("groups rows into tours and orders each by visit_order, not row order", () => {
    const routes = resolveRoutes([
      result([stop(1, 2, 3, 4), stop(0, 2, 1, 1), stop(1, 1, 2, 2), stop(0, 1, 0.5, 0.5)]),
    ]);
    expect(routes.routes.map((r) => r.index)).toEqual([0, 1]);
    expect(routes.routes[0]?.stops.map((s) => s.order)).toEqual([1, 2]);
    expect(routes.routes[1]?.stops.map((s) => s.order)).toEqual([1, 2]);
    expect(routes.stopCount).toBe(4);
  });

  it("keeps the per-tour distance and load, and the vehicle capacity", () => {
    const routes = resolveRoutes([result([stop(0, 1, 1, 1)])]);
    expect(routes.routes[0]?.distance).toBe(12.5);
    expect(routes.routes[0]?.loadMinutes).toBe(300);
    expect(routes.capacityMinutes).toBe(480);
  });

  it("skips a row with no coordinates instead of drawing it at the origin", () => {
    // A stop plotted at (0,0) would land on top of the depot and look like a
    // leg that does not exist.
    const routes = resolveRoutes([
      result([stop(0, 1, 1, 1), { route: 0, visit_order: 2, stop: "broken", x: null, y: 2 }]),
    ]);
    expect(routes.stopCount).toBe(1);
    expect(routes.previewCount).toBe(2);
  });

  it("flags a downsampled preview, because a missing stop becomes a false leg", () => {
    const routes = resolveRoutes([result([stop(0, 1, 1, 1)], 24)]);
    expect(routes.truncated).toBe(true);
    expect(routes.rowCount).toBe(24);
  });

  it("distinguishes no result message from a zero-row result", () => {
    expect(resolveRoutes([]).empty).toBe(true);
    expect(resolveRoutes([result([], 0)]).empty).toBe(false);
  });
});

describe("the decorative layer", () => {
  it("produces exactly as many stops as the instance says", () => {
    expect(decorativeStops(24)).toHaveLength(24);
    expect(decorativeStops(0)).toHaveLength(0);
    expect(decorativeStops(-3)).toHaveLength(0);
  });

  it("is deterministic in the stop count alone", () => {
    expect(decorativeStops(12)).toEqual(decorativeStops(12));
  });

  it("assigns every stop to exactly one tour", () => {
    const stops = decorativeStops(24);
    const routes = decorativeRoutes(3, stops, 3);
    const assigned = routes.flat();
    expect(assigned).toHaveLength(24);
    expect(new Set(assigned).size).toBe(24);
  });

  it("re-links between pulses — that redraw IS the incumbent", () => {
    const stops = decorativeStops(24);
    expect(JSON.stringify(decorativeRoutes(4, stops, 3))).not.toBe(
      JSON.stringify(decorativeRoutes(5, stops, 3)),
    );
  });

  it("is stable within one pulse, so a re-render does not reshuffle", () => {
    const stops = decorativeStops(24);
    expect(decorativeRoutes(4, stops, 3)).toEqual(decorativeRoutes(4, stops, 3));
  });

  it("survives a degenerate fleet rather than dividing by zero", () => {
    expect(decorativeRoutes(1, decorativeStops(5), 0)).toHaveLength(1);
    expect(decorativeRoutes(1, [], 3).flat()).toHaveLength(0);
  });
});

describe("projection", () => {
  it("keeps the depot inside the box even when no stop is near it", () => {
    // Every tour starts and ends at the origin; a view fitted to the stops
    // alone can push the depot out of its own picture.
    const { project } = projection([{ x: 40, y: 40 }, { x: 60, y: 55 }], 240, 16);
    const depot = project({ x: 0, y: 0 });
    expect(depot.x).toBeGreaterThanOrEqual(0);
    expect(depot.x).toBeLessThanOrEqual(240);
    expect(depot.y).toBeGreaterThanOrEqual(0);
    expect(depot.y).toBeLessThanOrEqual(240);
  });

  it("flips y, because SVG grows downward and a flipped map is worse than none", () => {
    const { project } = projection([{ x: 0, y: 10 }], 100, 0);
    expect(project({ x: 0, y: 10 }).y).toBeLessThan(project({ x: 0, y: 0 }).y);
  });

  it("does not divide by zero for a single stop at the depot", () => {
    const { project } = projection([{ x: 0, y: 0 }], 240, 16);
    const point = project({ x: 0, y: 0 });
    expect(Number.isFinite(point.x)).toBe(true);
    expect(Number.isFinite(point.y)).toBe(true);
  });
});

describe("tourPath", () => {
  it("closes the tour at the depot at both ends", () => {
    const identity = (p: { x: number; y: number }) => p;
    const path = tourPath([{ x: 1, y: 1 }, { x: 2, y: 2 }], { x: 0, y: 0 }, identity);
    expect(path).toBe("0.00,0.00 1.00,1.00 2.00,2.00 0.00,0.00");
  });
});
