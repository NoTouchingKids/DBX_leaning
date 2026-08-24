/**
 * What the route fan draws: the instance's shape while the run is live, the
 * real tours once results exist, and the decorative layer in between.
 *
 * ## Why the shape comes out of the LOG stream
 *
 * Same reason as `gurobi_scheduling`: `ModelViewProps` carries no config, and
 * the model states its instance in its own `input`-phase log line —
 *
 *     routing 24 stops with 3 vehicles, 480 service-minutes each (1610 required),
 *     at 1.35 per unit distance
 *
 * — which is also the ONLY place the two numbers that explain an INFEASIBLE
 * routing run appear. `vehicles x capacity_minutes < total_service_minutes` is
 * a one-field mistake on the trigger form, and the difference between showing
 * "1440 available / 1610 required" and showing a red box is the difference
 * between a user fixing it in ten seconds and filing a bug. Parsing is
 * best-effort and falls back to a plainly labelled default.
 *
 * ## What is NOT here, on purpose
 *
 * `cuts_added` and `separation_calls` — the numbers that describe how much of
 * the run was separation rather than search — live on the model and reach the
 * results and the DEBUG logs, never a `progress` message. There is no live cut
 * counter in this view, and there should not be one until the driver emits it.
 */

import type { LogMessage, ResultMessage } from "@/lib/envelope";

export interface RoutingShape {
  stopCount: number;
  vehicles: number;
  /** Service-minute budget per vehicle. Null when the log line is unparsed. */
  capacityMinutes: number | null;
  /** Service minutes the stops actually demand. */
  requiredMinutes: number | null;
  source: "log" | "results" | "default";
}

/** The model's own defaults, used only until its build log arrives. */
export const DEFAULT_ROUTING_SHAPE: RoutingShape = {
  stopCount: 24,
  vehicles: 3,
  capacityMinutes: null,
  requiredMinutes: null,
  source: "default",
};

const INSTANCE_LINE =
  /routing\s+(\d+)\s+stops\s+with\s+(\d+)\s+vehicles,\s*([\d.]+)\s*service-minutes\s*each\s*\(([\d.]+)\s*required\)/i;

export function parseRoutingShape(logs: readonly LogMessage[]): RoutingShape {
  for (let i = logs.length - 1; i >= 0; i -= 1) {
    const match = INSTANCE_LINE.exec(logs[i]?.message ?? "");
    if (!match) continue;
    const stopCount = Number(match[1]);
    const vehicles = Number(match[2]);
    const capacityMinutes = Number(match[3]);
    const requiredMinutes = Number(match[4]);
    if (stopCount > 0 && vehicles > 0) {
      return {
        stopCount,
        vehicles,
        capacityMinutes: Number.isFinite(capacityMinutes) ? capacityMinutes : null,
        requiredMinutes: Number.isFinite(requiredMinutes) ? requiredMinutes : null,
        source: "log",
      };
    }
  }
  return DEFAULT_ROUTING_SHAPE;
}

/** Whether the instance is over-subscribed on service minutes alone — the one
 *  infeasibility a user can diagnose without reading the model. Null when the
 *  log line has not been seen, which is NOT the same as "it is fine". */
export function capacityShortfall(shape: RoutingShape): number | null {
  if (shape.capacityMinutes === null || shape.requiredMinutes === null) return null;
  return shape.requiredMinutes - shape.vehicles * shape.capacityMinutes;
}

/* ================================================================== *
 * The real tours, from result rows
 * ================================================================== */

export interface Point {
  x: number;
  y: number;
}

export interface RouteStop extends Point {
  name: string;
  /** `visit_order`, 1-based, as the model emits it. */
  order: number;
}

export interface ResolvedRoute {
  index: number;
  stops: readonly RouteStop[];
  /** Total distance for the whole tour, repeated on every row of it. */
  distance: number | null;
  loadMinutes: number | null;
}

export interface ResolvedRoutes {
  routes: readonly ResolvedRoute[];
  stopCount: number;
  rowCount: number;
  previewCount: number;
  /** `preview` is a downsample of the durable rows: tours may be missing
   *  stops. Drawing a shortcut between two stops as if it were a leg would be
   *  a lie about the solution, so this is surfaced. */
  truncated: boolean;
  empty: boolean;
  capacityMinutes: number | null;
}

const EMPTY_ROUTES: ResolvedRoutes = {
  routes: [],
  stopCount: 0,
  rowCount: 0,
  previewCount: 0,
  truncated: false,
  empty: true,
  capacityMinutes: null,
};

function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * Group `result.preview` rows into tours.
 *
 * Every field is re-validated: `preview` is `Array<Record<string, unknown>>`
 * on the wire and nothing server-side looks inside it. A row missing its
 * coordinates costs that row, not the picture.
 */
export function resolveRoutes(results: readonly ResultMessage[]): ResolvedRoutes {
  if (results.length === 0) return EMPTY_ROUTES;

  const grouped = new Map<number, RouteStop[]>();
  const distance = new Map<number, number | null>();
  const load = new Map<number, number | null>();
  let rowCount = 0;
  let previewCount = 0;
  let capacityMinutes: number | null = null;

  for (const result of results) {
    rowCount += result.row_count;
    for (const row of result.preview) {
      previewCount += 1;
      const index = asNumber(row["route"]);
      const x = asNumber(row["x"]);
      const y = asNumber(row["y"]);
      const order = asNumber(row["visit_order"]);
      const name = typeof row["stop"] === "string" ? row["stop"] : null;
      if (index === null || x === null || y === null || order === null || name === null) continue;

      const stops = grouped.get(index) ?? [];
      stops.push({ name, x, y, order });
      grouped.set(index, stops);
      if (!distance.has(index)) distance.set(index, asNumber(row["route_distance"]));
      if (!load.has(index)) load.set(index, asNumber(row["route_load_minutes"]));
      capacityMinutes ??= asNumber(row["vehicle_capacity_minutes"]);
    }
  }

  const routes = [...grouped.entries()]
    .map(([index, stops]) => ({
      index,
      // `visit_order` is the tour, not the row order: the preview's row order
      // survives the server's downsampling but its completeness does not.
      stops: [...stops].sort((a, b) => a.order - b.order),
      distance: distance.get(index) ?? null,
      loadMinutes: load.get(index) ?? null,
    }))
    .sort((a, b) => a.index - b.index);

  return {
    routes,
    stopCount: routes.reduce((total, route) => total + route.stops.length, 0),
    rowCount,
    previewCount,
    truncated: previewCount < rowCount,
    empty: false,
    capacityMinutes,
  };
}

/* ================================================================== *
 * The decorative layer
 * ================================================================== */

/** The same golden angle `models/gurobi_routing/instance.py` uses to spread
 *  stops around the depot. Borrowing the rule means the decorative scatter has
 *  the character of a real instance without pretending to BE one. */
const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5));

/**
 * A plausible stop field around a depot at the origin.
 *
 * Decorative — no progress message carries stop coordinates, and the real ones
 * only arrive with the results. Deterministic in `count` alone so every tab
 * draws the same field and a test can assert on it.
 */
export function decorativeStops(count: number): Point[] {
  return Array.from({ length: Math.max(0, count) }, (_, i) => {
    const angle = i * GOLDEN_ANGLE;
    // sqrt keeps the density even instead of crowding the depot.
    const radius = Math.sqrt((i + 1) / Math.max(1, count));
    return { x: radius * Math.cos(angle), y: radius * Math.sin(angle) };
  });
}

/**
 * Assign the decorative stops to `vehicles` tours for frame `pulse`.
 *
 * Angular sectors, rotated by the pulse, with each tour ordered outward from
 * the depot — which is what a real solution mostly looks like and what makes
 * the re-link on a new incumbent read as "the routes changed" rather than as
 * noise. The PARTITION is decorative; the fact that it only ever changes when
 * `solution_count` increments is not.
 */
export function decorativeRoutes(
  pulse: number,
  stops: readonly Point[],
  vehicles: number,
): number[][] {
  const lanes = Math.max(1, vehicles);
  const routes: number[][] = Array.from({ length: lanes }, () => []);
  if (stops.length === 0) return routes;

  const rotation = pulse * GOLDEN_ANGLE;
  stops.forEach((stop, i) => {
    const angle = Math.atan2(stop.y, stop.x) + rotation;
    const turns = angle / (Math.PI * 2);
    const lane = Math.floor(((turns % 1) + 1) % 1 * lanes) % lanes;
    routes[lane]?.push(i);
  });

  for (const route of routes) {
    route.sort((a, b) => radius(stops[a]) - radius(stops[b]));
  }
  return routes;
}

function radius(point: Point | undefined): number {
  if (point === undefined) return 0;
  return Math.hypot(point.x, point.y);
}

/* ================================================================== *
 * Projection into the SVG box
 * ================================================================== */

export interface Projection {
  project: (point: Point) => Point;
}

/**
 * Fit points (plus the depot at the origin, always) into a `size` x `size` box.
 *
 * The depot is included unconditionally because every tour starts and ends
 * there: a view scaled to the stops alone can push the depot outside its own
 * picture, and the legs to and from it are most of the cost.
 */
export function projection(points: readonly Point[], size: number, padding: number): Projection {
  const xs = [0, ...points.map((p) => p.x)];
  const ys = [0, ...points.map((p) => p.y)];
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const span = Math.max(maxX - minX, maxY - minY, 1e-9);
  const scale = (size - padding * 2) / span;
  // Centre the shorter axis so a lopsided instance is not glued to one edge.
  const offsetX = padding + (size - padding * 2 - (maxX - minX) * scale) / 2;
  const offsetY = padding + (size - padding * 2 - (maxY - minY) * scale) / 2;

  return {
    project: (point) => ({
      x: offsetX + (point.x - minX) * scale,
      // SVG y grows downward; a map that flips north and south is worse than
      // no map.
      y: size - (offsetY + (point.y - minY) * scale),
    }),
  };
}

/** An SVG polyline `points` string for depot -> stops -> depot. */
export function tourPath(stops: readonly Point[], depot: Point, project: (p: Point) => Point): string {
  const path = [depot, ...stops, depot].map(project);
  return path.map((p) => `${p.x.toFixed(2)},${p.y.toFixed(2)}`).join(" ");
}
