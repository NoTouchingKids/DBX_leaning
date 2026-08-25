/**
 * The signature: a depot with a fan of vehicle tours.
 *
 * ## Why a fan and not a grid
 *
 * `gurobi_routing` shares its driver, its payload and its solver with
 * `gurobi_scheduling`, so nothing in the telemetry distinguishes them. What
 * distinguishes them is what they are solving, and the signature has to make
 * that legible at a glance: scheduling fills a staff x day timetable, routing
 * connects stops into closed tours out of a depot. Reusing the grid here would
 * make two different problems look like one.
 *
 * ## The hook the cadence hangs on
 *
 * The same one, for a reason specific to this model. Every incumbent here has
 * passed through `RoutingModel.gurobi_callback`: Gurobi only accepts a
 * candidate once separation returns without a violated rounded-capacity cut,
 * so a `solution_count` increment is not merely "a better number" — it is a
 * complete, connected, capacity-feasible set of routes being accepted. Re-
 * linking the fan on exactly those events is therefore the most faithful thing
 * this animation can do without per-solution data that no message carries.
 *
 * Positions and which stop joins which tour are DECORATIVE while the run is
 * live. On a settled run with rows, the tours are the real ones: `results()`
 * carries every stop's `x`/`y` and its `visit_order`, and the depot is the
 * origin (`instance.py`, `depot = (0.0, 0.0)`).
 */

import { motion } from "motion/react";

import { formatCount, formatMetric } from "@/lib/format";
import { isAnimating, isSettled, type ModelViewProps } from "../contract";
import { usePrefersReducedMotion } from "../useReducedMotion";
import { deriveMipSeries, incumbentActivity, terminalDetail } from "../gurobi_shared/mipSeries";
import { SignatureHeader } from "../gurobi_shared/SignatureHeader";
import { TONE_VAR } from "../gurobi_shared/tones";
import { describeRoutingState } from "./stateCopy";
import {
  decorativeRoutes,
  decorativeStops,
  parseRoutingShape,
  projection,
  resolveRoutes,
  tourPath,
  type Point,
} from "./routing";

const BOX = 240;
const PADDING = 16;
const DEPOT: Point = { x: 0, y: 0 };

/** Tours are told apart by weight, not by hue: the palette's colours all mean
 *  something (state), and spending five of them on "vehicle 3" would make a
 *  route look like a warning. */
const ROUTE_OPACITY = [1, 0.78, 0.6, 0.46, 0.36] as const;

export function RouteFan({ state, snapshot }: ModelViewProps) {
  const reduced = usePrefersReducedMotion();

  const shape = parseRoutingShape(snapshot.logs);
  const routes = resolveRoutes(snapshot.results);
  const activity = incumbentActivity(deriveMipSeries(snapshot.progress));

  const copy = describeRoutingState(state, {
    shape,
    routes,
    solutionCount: activity.solutionCount,
    detail: terminalDetail(snapshot.statuses),
  });

  const resolved = isSettled(state) && routes.routes.length > 0;

  // Real tours when they exist; a decorative fan of the right SIZE otherwise.
  const stops: Point[] = resolved
    ? routes.routes.flatMap((route) => route.stops)
    : decorativeStops(shape.stopCount);
  const tours: Point[][] = resolved
    ? routes.routes.map((route) => [...route.stops])
    : decorativeRoutes(activity.pulses, stops, shape.vehicles).map((lane) =>
        lane.map((i) => stops[i]).filter((point): point is Point => point !== undefined),
      );

  const { project } = projection(stops, BOX, PADDING);
  const depot = project(DEPOT);
  const stroke = TONE_VAR[copy.tone];
  // INFEASIBLE and FAILED are flat states: the tours carry no meaning past the
  // end of a run that produced none, so they are simply not drawn.
  const drawTours = resolved || (!isSettled(state) && state !== null);

  const readout = [
    activity.solutionCount === null ? null : `accepted ${formatCount(activity.solutionCount)}`,
    activity.latest?.nodesExplored == null
      ? null
      : `nodes ${formatCount(activity.latest.nodesExplored)}`,
    activity.latest?.gapPercent == null
      ? null
      : `gap ${formatMetric(Number(activity.latest.gapPercent.toFixed(2)))}%`,
  ]
    .filter((part) => part !== null)
    .join("  ·  ");

  return (
    <div>
      <SignatureHeader
        copy={copy}
        readout={readout === "" ? undefined : readout}
        animating={isAnimating(state)}
        reducedMotion={reduced}
      />

      <div className="rounded-lg border border-line bg-paper">
        <svg
          viewBox={`0 0 ${BOX} ${BOX}`}
          preserveAspectRatio="xMidYMid meet"
          className="h-[220px] w-full"
          role="img"
          aria-label={`${copy.title}. ${copy.detail}`}
        >
          {drawTours &&
            tours.map((tour, index) =>
              tour.length === 0 ? null : (
                <motion.polyline
                  // Keying on the pulse is what makes a new incumbent redraw
                  // the tour instead of tweening between two unrelated shapes.
                  key={`tour-${index}-${resolved ? "final" : activity.pulses}`}
                  points={tourPath(tour, DEPOT, project)}
                  fill="none"
                  stroke={stroke}
                  strokeWidth={1.6}
                  strokeLinejoin="round"
                  strokeOpacity={ROUTE_OPACITY[Math.min(index, ROUTE_OPACITY.length - 1)]}
                  initial={reduced ? false : { pathLength: 0, opacity: 0.2 }}
                  animate={{ pathLength: 1, opacity: 1 }}
                  transition={reduced ? { duration: 0 } : { duration: 0.55, ease: "easeOut" }}
                />
              ),
            )}

          {stops.map((stop, index) => {
            const point = project(stop);
            return (
              <circle
                key={`stop-${index}`}
                cx={point.x}
                cy={point.y}
                r={2.6}
                fill={drawTours ? stroke : "var(--c-paper)"}
                stroke={stroke}
                strokeWidth={1}
                opacity={drawTours ? 1 : 0.55}
              />
            );
          })}

          {/* The depot: square, so it is never mistaken for a stop. */}
          <rect
            x={depot.x - 4}
            y={depot.y - 4}
            width={8}
            height={8}
            fill="var(--c-raised)"
            stroke={stroke}
            strokeWidth={1.6}
          />
        </svg>
      </div>

      <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 font-mono text-[0.64rem] text-faint">
        <span>
          {formatCount(resolved ? routes.stopCount : shape.stopCount)} stops ·{" "}
          {formatCount(resolved ? routes.routes.length : shape.vehicles)} vehicles
        </span>
        {shape.capacityMinutes !== null && (
          <span>
            {formatMetric(shape.capacityMinutes)} service-minutes each
            {shape.requiredMinutes === null
              ? ""
              : ` · ${formatMetric(shape.requiredMinutes)} required`}
          </span>
        )}
        {resolved && (
          <span>
            total distance{" "}
            {formatMetric(
              routes.routes.reduce((sum, route) => sum + (route.distance ?? 0), 0),
            )}
          </span>
        )}
        {shape.source === "default" && !resolved && (
          <span>instance size is the model default — this run has not logged its own yet</span>
        )}
      </div>

      {resolved && routes.truncated && (
        <p className="mt-1.5 text-[0.68rem] text-warn">
          Showing {formatCount(routes.previewCount)} of {formatCount(routes.rowCount)} written stops
          — `result.preview` is downsampled server-side, so some legs drawn here skip a stop that is
          in the durable table.
        </p>
      )}
    </div>
  );
}
