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
 *
 * ## The lifecycle phases of `motion.ts`, as frames
 *
 *   idle       the stop field at rest — dim, unlinked, no depot wave. An empty
 *              map. Nothing moves, because nothing has been asked.
 *   starting   the field lifts to full in one staggered pass while a single
 *              wave leaves the depot over DURATION.inhale, and then HOLDS
 *              there. The hold is the point: a cold job sits in this phase for
 *              tens of seconds and must not sit in the idle frame while it does.
 *   running    two sub-states, split on a real event rather than a timer.
 *              Before the first incumbent nothing is linked and a wave leaves
 *              the depot once per DURATION.ambient — the search reaching out,
 *              which is exactly what the RUNNING copy claims. After it, the fan
 *              is linked and one lit segment travels each tour per
 *              DURATION.ambient, the vehicles spread evenly around the cycle.
 *   settled    one flat frame. Real tours draw themselves once if the run wrote
 *              rows; otherwise the field alone in the terminal tone. Nothing
 *              loops, drifts or pulses past the end of a run.
 *
 * Two things the previous version got wrong, both easy to miss:
 *
 *  - It drew a full fan from the moment a run existed, including at QUEUED and
 *    before Gurobi had accepted anything. A closed set of tours on screen while
 *    the header says "no connected, capacity-feasible routing found yet" is the
 *    picture contradicting the words. Tours now require `pulses > 0`.
 *  - Nothing moved during RUNNING between incumbents, and incumbents can be
 *    minutes apart or absent entirely — a long presolve emits no progress
 *    messages at all. A live solve was pixel-identical to a dead frame.
 */

import { motion } from "motion/react";

import { formatCount, formatMetric } from "@/lib/format";
import { isAnimating, isSettled, type ModelViewProps } from "../contract";
import { DURATION, EASE, phaseOf, staggerFor } from "../motion";
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
/** Half the depot square's side. Waves start at its edge, not at its centre. */
const DEPOT_HALF = 4;

/** Tours are told apart by weight, not by hue: the palette's colours all mean
 *  something (state), and spending five of them on "vehicle 3" would make a
 *  route look like a warning. */
const ROUTE_OPACITY = [1, 0.78, 0.6, 0.46, 0.36] as const;

/** Stop radius at rest and awake. The difference is small on purpose — this is
 *  the field waking up, not a set of buttons growing. */
const STOP_R = { rest: 2.2, awake: 2.8 } as const;
const STOP_REST_OPACITY = 0.5;

/** How much of a tour the travelling segment lights at once, as a fraction of
 *  the whole loop. Below about a tenth it reads as a firefly rather than as
 *  something moving along the route. */
const GLINT = 0.14;

/** Peak opacity of a depot wave. Deliberately faint: this is the one element
 *  on screen that repeats forever, and at full strength it competes with the
 *  tours for the eye every 2.4 seconds. */
const WAVE_PEAK = 0.3;

export function RouteFan({ state, snapshot }: ModelViewProps) {
  const reduced = usePrefersReducedMotion();
  const phase = phaseOf(state);

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
  /** Tours exist on screen only when a routing exists in fact: the real one
   *  after the run, or an accepted candidate during it. This is also what makes
   *  INFEASIBLE and FAILED flat — a run that produced no routing leaves no
   *  per-tour meaning behind for the terminal frame to keep. */
  const linked = resolved || (phase === "running" && activity.pulses > 0);
  /** Searching, with nothing accepted yet — a state a hard instance can sit in
   *  for the whole run, so it gets a frame of its own rather than a blank fan. */
  const searching = phase === "running" && !linked;
  const awake = phase !== "idle";

  // Real tours when they exist; a decorative fan of the right SIZE otherwise.
  const stops: Point[] = resolved
    ? routes.routes.flatMap((route) => route.stops)
    : decorativeStops(shape.stopCount);
  const tours: Point[][] = resolved
    ? routes.routes.map((route) => [...route.stops])
    : linked
      ? decorativeRoutes(activity.pulses, stops, shape.vehicles).map((lane) =>
          lane.map((i) => stops[i]).filter((point): point is Point => point !== undefined),
        )
      : [];

  const { project } = projection(stops, BOX, PADDING);
  const depot = project(DEPOT);
  const stroke = TONE_VAR[copy.tone];

  const projected = stops.map(project);
  // How far a depot wave has to travel to cover the field. Measured rather than
  // assumed: the projection centres the stops' bounding box, so a lopsided
  // instance puts the depot off-centre and a hardcoded radius would either stop
  // short or spill out of the frame.
  const reach =
    projected.reduce((max, p) => Math.max(max, Math.hypot(p.x - depot.x, p.y - depot.y)), 0) ||
    BOX / 2 - PADDING;

  const paths = tours.map((tour) => (tour.length === 0 ? null : tourPath(tour, DEPOT, project)));
  const stopStagger = reduced ? 0 : staggerFor(stops.length);
  const tourStagger = reduced ? 0 : staggerFor(paths.length);

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
          {/* Both waves are purely ambient, so reduced motion drops them
              entirely — neither carries a fact the header does not state. */}
          {!reduced && phase === "starting" && (
            <DepotWave at={depot} reach={reach} stroke={stroke} loop={false} />
          )}
          {!reduced && searching && (
            <DepotWave at={depot} reach={reach} stroke={stroke} loop />
          )}

          {paths.map((points, index) =>
            points === null ? null : (
              <motion.polyline
                // Keying on the pulse is what makes a new incumbent redraw
                // the tour instead of tweening between two unrelated shapes.
                key={`tour-${index}-${resolved ? "final" : activity.pulses}`}
                points={points}
                fill="none"
                stroke={stroke}
                strokeWidth={1.6}
                strokeLinejoin="round"
                strokeOpacity={ROUTE_OPACITY[Math.min(index, ROUTE_OPACITY.length - 1)]}
                initial={reduced ? false : { pathLength: 0, opacity: 0.2 }}
                animate={{ pathLength: 1, opacity: 1 }}
                transition={
                  reduced
                    ? { duration: 0 }
                    : {
                        duration: DURATION.slow,
                        ease: EASE.decelerate,
                        delay: index * tourStagger,
                      }
                }
              />
            ),
          )}

          {/* The travelling segment. Only while the run is live: per contract,
              nothing may still be moving once it has ended. */}
          {!reduced &&
            phase === "running" &&
            paths.map((points, index) =>
              points === null ? null : (
                <motion.polyline
                  key={`glint-${index}-${activity.pulses}`}
                  points={points}
                  fill="none"
                  stroke={stroke}
                  strokeWidth={3}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeOpacity={ROUTE_OPACITY[Math.min(index, ROUTE_OPACITY.length - 1)]}
                  initial={{ pathLength: GLINT, pathSpacing: 1 - GLINT, pathOffset: 0 }}
                  animate={{ pathOffset: 1 }}
                  transition={{
                    duration: DURATION.ambient,
                    // Linear, and it is the one place in this file that is.
                    // A loop that eases pumps once per cycle, which over a
                    // ten-minute solve is the thing that becomes irritating.
                    ease: "linear",
                    repeat: Infinity,
                    // Spread the vehicles evenly around one cycle rather than
                    // staggering them by a fixed step: `staggerFor` caps at
                    // 40ms, which against a 2.4s loop is no offset at all and
                    // would leave them running as a chorus line. The wait for
                    // the tour to finish drawing is what keeps a segment off a
                    // line that is not there yet.
                    delay: DURATION.slow + (index / paths.length) * DURATION.ambient,
                  }}
                />
              ),
            )}

          {projected.map((point, index) => (
            <motion.circle
              key={`stop-${index}`}
              cx={point.x}
              cy={point.y}
              // Filled once a routing exists, hollow while it does not. Not
              // animated: the swap is a fact changing, and `fill` here is a
              // custom property that cannot be interpolated anyway.
              fill={linked ? stroke : "var(--c-paper)"}
              stroke={stroke}
              strokeWidth={1}
              // `initial` is the RESTING look rather than nothing at all. That
              // is what makes the lift fire both ways: as a transition when the
              // phase leaves idle, and as an entrance for a view that mounted
              // straight into STARTING. The component stays mounted for a whole
              // run, so anything driven by mount alone plays in whichever phase
              // happened to be current when the run page was opened — which for
              // a run watched from the start is idle, with nothing to say.
              initial={{ r: STOP_R.rest, opacity: STOP_REST_OPACITY }}
              animate={{
                r: awake ? STOP_R.awake : STOP_R.rest,
                opacity: awake ? 1 : STOP_REST_OPACITY,
              }}
              transition={{
                duration: reduced ? 0 : DURATION.base,
                ease: EASE.decelerate,
                delay: index * stopStagger,
              }}
            />
          ))}

          {/* The depot: square, so it is never mistaken for a stop. */}
          <rect
            x={depot.x - DEPOT_HALF}
            y={depot.y - DEPOT_HALF}
            width={DEPOT_HALF * 2}
            height={DEPOT_HALF * 2}
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

/**
 * One wave leaving the depot.
 *
 * Two callers, one shape: the STARTING inhale runs it once over
 * DURATION.inhale, the pre-incumbent search loops it at DURATION.ambient.
 *
 * Radius and opacity share a single transition, with a midpoint keyframe added
 * to `r` purely so the two have the same keyframe count and can share `times`.
 * The obvious alternative — a per-property transition each with its own
 * `repeat: Infinity` — is two independently scheduled loops of nominally equal
 * length, and a ring whose fade has slipped out of step with its expansion
 * reads as a glitch rather than as a wave.
 */
function DepotWave({
  at,
  reach,
  stroke,
  loop,
}: {
  at: Point;
  reach: number;
  stroke: string;
  /** Ambient and endless, versus the single inhale that says "asked". */
  loop: boolean;
}) {
  const inner = DEPOT_HALF + 1;
  const mid = inner + (reach - inner) * 0.55;
  return (
    <motion.circle
      cx={at.x}
      cy={at.y}
      fill="none"
      stroke={stroke}
      strokeWidth={1}
      initial={{ r: inner, opacity: 0 }}
      animate={{ r: [inner, mid, reach], opacity: [0, WAVE_PEAK, 0] }}
      transition={{
        duration: loop ? DURATION.ambient : DURATION.inhale,
        // Quick to appear, slow to spread. The long tail is what stops a
        // looping wave reading as a blink: it is visible for most of the cycle
        // and lands back at zero opacity, so the restart is never seen.
        times: [0, 0.22, 1],
        ease: EASE.decelerate,
        ...(loop ? { repeat: Infinity } : {}),
      }}
    />
  );
}
