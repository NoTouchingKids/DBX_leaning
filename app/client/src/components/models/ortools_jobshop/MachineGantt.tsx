/**
 * The signature: a machine-by-machine Gantt of the shop floor, with the
 * makespan pulling in.
 *
 * ## Why a Gantt, and why it is the right one here
 *
 * A job-shop solution IS an assignment of operations to machines over time,
 * and `results_ortools_jobshop` stores exactly that grain — one row per
 * scheduled operation, with `machine_id`, `start_minute`, `duration_minutes`
 * and `job_label`. So on a settled run this is not an illustration of the
 * answer, it is the answer, rendered without a join. That is the same property
 * `gurobi_scheduling`'s grid has, and it is why neither model needs a separate
 * results disclosure.
 *
 * ## The phases of `motion.ts`, as frames
 *
 *   idle       lanes at the instance's machine count, empty and dimmed.
 *              Nothing has been asked for and nothing moves.
 *   starting   the lanes lift to full in one pass down the floor over
 *              DURATION.inhale while a single sweep of light crosses the open
 *              horizon, then HOLD. A cold job sits here for tens of seconds,
 *              so the held frame is the lift itself — visibly not idle — and
 *              not motion that has already finished.
 *   running    two sub-states, split on a real event rather than on a clock.
 *              Before CP-SAT's first feasible schedule the floor stays EMPTY
 *              and only the sweep loops at DURATION.ambient — the search
 *              working across an open horizon, which is exactly what the
 *              RUNNING copy claims. After it, a makespan edge appears at the
 *              right and steps inward once per observed improvement, the floor
 *              compressing into it and re-laying on the same event, with the
 *              sweep continuing inside the new horizon between them.
 *   settled    one flat frame. The real schedule when the run wrote rows, the
 *              terminal tone otherwise. No sweep, no edge, no pulse.
 *
 * ## Three things the previous version got wrong
 *
 *  - **A full floor before the first solution.** Bars were gated on RUNNING
 *    alone, and `decorativeBars(0, …)` is not an empty floor — it lays a
 *    complete schedule — so a search that had not yet found anything feasible
 *    drew a busy shop under a header reading "Searching for a first schedule".
 *    Bars now require `solutions_found > 0`, which is CP-SAT saying a schedule
 *    exists.
 *  - **STARTING was pixel-identical to no run at all.** Both drew empty lanes
 *    at full weight, which is the one frame that has to hold for tens of
 *    seconds while a cold job spins up.
 *  - **Nothing moved between improvements.** This matters more here than on
 *    the two Gurobi views and for the reason this model exists to contrast
 *    with them: a MIP callback chatters, CP-SAT's fires two to twenty times in
 *    a whole run. Keying every pixel to improvements meant a live solve was
 *    indistinguishable from a dead page for minutes at a stretch.
 *
 * ## Where this reads differently from the two Gurobi views
 *
 * Two things, and both are deliberately made legible rather than smoothed
 * over. A still Gurobi grid is ambiguous; a still floor HERE genuinely means
 * the search is not improving, which is why the sweep — and only the sweep —
 * keeps moving to say the stream is alive without claiming progress. And
 * `percent_complete` is populated on this model where it is permanently null
 * on those two, which makes it more dangerous, not less, because it is a TIME
 * fraction. The clock bar below the lanes says so on its own line rather than
 * leaving the honesty note to carry it alone.
 */

import { motion } from "motion/react";

import type { UiRunState } from "@/lib/envelope";
import { formatCount, formatMetric } from "@/lib/format";
import { isAnimating, isSettled, type ModelViewProps } from "../contract";
import { DURATION, EASE, phaseOf, SPRING, STAGGER, staggerFor, type MotionPhase } from "../motion";
import { usePrefersReducedMotion } from "../useReducedMotion";
import {
  decorativeBars,
  layoutGantt,
  PLOT_WIDTH,
  resolveInstance,
  resolveSchedule,
  type GanttLane,
} from "./schedule";
import { deriveJobshopSeries, solveActivity, solverClock, terminalDetail } from "./series";
import { describeJobshopState } from "./stateCopy";
import { TONE_DOT, TONE_FILL, TONE_SOFT, TONE_TEXT, type StateCopy, type Tone } from "./tone";

/** Enough lanes for the five stages plus headroom for an instance that grows
 *  one. Past this the floor stops being readable and the rest are counted. */
const MAX_LANES = 12;

const percent = (units: number) => `${(units / PLOT_WIDTH) * 100}%`;

/** The lanes before a run exists. Low enough to read as "not asked for yet",
 *  high enough that the shop floor's shape is still legible. */
const REST_OPACITY = 0.45;

/**
 * Where the makespan edge sits after `improvements` improving solutions, as a
 * percentage of the floor's width.
 *
 * DECORATIVE in magnitude, and it has to be. `incumbent` is a real number on
 * every sample, but a makespan in minutes means nothing without a scale to
 * draw it against, and the convergence chart below this is where a real one
 * belongs. What is NOT decorative is the shape: monotone inward, exactly one
 * step per improvement, with diminishing returns and a floor it never reaches.
 * All three are structural facts about this model rather than choices — CP-SAT's
 * callback fires only on a STRICTLY better solution, so the makespan only ever
 * falls; and no schedule beats the busiest machine's total load however long
 * the search runs, which is the same floor `stateCopy.ts` uses to explain an
 * INFEASIBLE deadline.
 */
const HORIZON_FLOOR = 56;
const HORIZON_DECAY = 0.72;

function horizonFor(improvements: number): number {
  if (improvements <= 0) return 100;
  return HORIZON_FLOOR + (100 - HORIZON_FLOOR) * HORIZON_DECAY ** improvements;
}

/**
 * Sweep geometry, in percentages of the horizon it travels.
 *
 * Percentages rather than pixels because the lane column is `1fr` and this
 * file measures nothing — `x` resolves a percentage against the element's OWN
 * width, so the travel is expressed in multiples of that instead of in
 * multiples of the container. The peak opacity is low because this is the one
 * element on screen that repeats forever, and anything with real presence at
 * that cadence becomes the thing you cannot stop looking at.
 */
const SWEEP_PCT = 14;
const SWEEP_PEAK = 0.24;
const SWEEP_TRAVEL = ((100 + SWEEP_PCT) / SWEEP_PCT) * 100;

export function MachineGantt({ state, snapshot }: ModelViewProps) {
  const reduced = usePrefersReducedMotion();
  const phase = phaseOf(state);

  const points = deriveJobshopSeries(snapshot.progress);
  const activity = solveActivity(points);
  const clock = solverClock(points);
  const schedule = resolveSchedule(snapshot.results);
  const shape = resolveInstance(snapshot.logs, activity.latest);

  // The floor resolves to real data whenever real data exists — which includes
  // a cancelled run, because `results()` is never gated on OPTIMAL and a
  // cancelled run keeps its incumbent.
  const resolved = isSettled(state) && schedule.operations.length > 0;
  const layout = layoutGantt(schedule, shape);

  const copy = describeJobshopState(state, {
    schedule,
    shape,
    solutionsFound: activity.solutionsFound ?? schedule.solutionsFound,
    improvements: activity.improvements,
    detail: terminalDetail(snapshot.statuses),
    // The final progress sample carries it, and so does every result row. The
    // rows are the more durable of the two: a dropped final sample on a
    // best-effort live path would otherwise lose the solver's own verdict.
    solverStatus: activity.solverStatus ?? schedule.solverStatus,
  });

  const lanes = layout.lanes.slice(0, MAX_LANES);
  const hiddenLanes = layout.lanes.length - lanes.length;

  /** A run exists at all. Deliberately not `phase !== "idle"`: `phaseOf` calls
   *  QUEUED idle, correctly — nothing may MOVE while a run waits for one of the
   *  five account-wide job slots — but dimming the floor back down between
   *  STARTING and RUNNING would read as the run being un-asked for a moment.
   *  The lift is a fact about whether a run exists; the phase decides what, if
   *  anything, animates. */
  const awake = state !== null;

  // A makespan exists only once a feasible schedule does, and on a hard
  // instance that is most of the run. `improvements` counts observed events and
  // rises with `solutions_found`, so the two agree; the gate reads off the
  // solver's own count because "CP-SAT has a schedule" is the claim being made.
  const solved = (activity.solutionsFound ?? 0) > 0;
  const horizon = phase === "running" && solved ? horizonFor(activity.improvements) : 100;

  // Only while the search is actually running AND has something to show. QUEUED
  // and STARTING have not begun the thing a busy floor would depict, and a
  // floor drawn before the first feasible schedule contradicts the header.
  const decorative =
    phase === "running" && solved ? decorativeBars(activity.improvements, lanes.length) : [];

  // Utilisation over a sampled subset understates every machine by an unknown
  // amount, so it is withheld rather than shown smaller than it is.
  const showUtilisation = resolved && !schedule.truncated && layout.span > 0;

  const readout = [
    schedule.makespan === null
      ? activity.latest?.incumbent == null
        ? null
        : `makespan ${formatMetric(activity.latest.incumbent)}`
      : `makespan ${formatCount(schedule.makespan)}`,
    activity.latest?.bestBound == null ? null : `bound ${formatMetric(activity.latest.bestBound)}`,
    activity.latest?.gapPercent == null
      ? null
      : `gap ${formatMetric(Number(activity.latest.gapPercent.toFixed(2)))}%`,
    activity.solutionsFound === null ? null : `sol ${formatCount(activity.solutionsFound)}`,
  ]
    .filter((part) => part !== null)
    .join("  ·  ");

  return (
    <div>
      <Header
        copy={copy}
        readout={readout === "" ? undefined : readout}
        phase={phase}
        reducedMotion={reduced}
      />

      <div
        className="rounded-lg border border-line bg-paper p-2.5"
        role="img"
        aria-label={`${copy.title}. ${copy.detail}`}
      >
        <div className="relative grid grid-cols-[68px_1fr_2.4rem] items-center gap-x-2 gap-y-1">
          {lanes.map((lane, index) => (
            <Lane
              key={lane.machineId}
              lane={lane}
              state={state}
              phase={phase}
              awake={awake}
              index={index}
              laneCount={lanes.length}
              resolved={resolved}
              tone={copy.tone}
              decorative={decorative.filter((bar) => bar.lane === lane.machineId)}
              pulse={activity.improvements}
              horizon={horizon}
              reducedMotion={reduced}
              utilisation={
                showUtilisation ? Math.round((lane.busyMinutes / layout.span) * 100) : null
              }
            />
          ))}

          {/* Last in the DOM so it passes OVER the lanes. It spans every lane
              row in the middle column, which is how one edge can cross the
              whole floor without this file knowing the column's pixel width. */}
          {(phase === "starting" || phase === "running") && lanes.length > 0 && (
            <motion.div
              className="pointer-events-none relative overflow-hidden"
              style={{
                gridColumn: "2",
                gridRow: `1 / span ${lanes.length}`,
                // `items-center` on the grid would otherwise collapse a
                // contentless item to zero height, and the edge has to cross
                // every lane.
                alignSelf: "stretch",
              }}
              initial={false}
              animate={{ width: `${horizon}%` }}
              transition={reduced ? { duration: 0 } : SPRING.soft}
              aria-hidden="true"
            >
              {/* Purely ambient, so it is gone entirely under reduced motion
                  rather than snapping: it states nothing the header does not
                  already say in words. Keyed on the phase so the single inhale
                  and the ambient loop are two animations, not one retimed. */}
              {!reduced && <SearchSweep key={phase} tone={copy.tone} loop={phase === "running"} />}
              {/* The makespan edge. Its arrival IS the first feasible schedule,
                  which is the one moment in a CP-SAT run worth marking. */}
              {solved && <div className="absolute inset-y-0 right-0 w-px bg-info" />}
            </motion.div>
          )}
        </div>

        {resolved && (
          <div className="mt-1.5 grid grid-cols-[68px_1fr_2.4rem] gap-x-2">
            <div />
            <div className="flex justify-between font-mono text-[0.6rem] text-faint">
              <span>0</span>
              <span>{formatCount(Math.round(layout.span / 2))}</span>
              <span>{formatCount(Math.round(layout.span))} min</span>
            </div>
            <div />
          </div>
        )}
      </div>

      <SolverClockBar clock={clock} animating={isAnimating(state)} reducedMotion={reduced} />

      <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 font-mono text-[0.64rem] text-faint">
        <span>
          {shape.jobs === null ? "?" : formatCount(shape.jobs)} jobs ·{" "}
          {shape.operations === null ? "?" : formatCount(shape.operations)} operations ·{" "}
          {formatCount(shape.machines)} machines
        </span>
        {shape.makespanLowerBound !== null && (
          <span>floor {formatCount(shape.makespanLowerBound)} min</span>
        )}
        {shape.deadlineMinutes !== null && (
          <span className={state === "INFEASIBLE" ? "text-warn" : undefined}>
            deadline {formatCount(shape.deadlineMinutes)} min
          </span>
        )}
        {shape.transactions !== null && (
          <span>{formatCount(shape.transactions)} sales transactions behind the batches</span>
        )}
        {schedule.dataSynthetic === true && <span className="text-warn">synthetic batches</span>}
        {shape.source === "default" && !resolved && (
          <span>shop floor is the model default — this run has not logged its own yet</span>
        )}
      </div>

      {hiddenLanes > 0 && (
        <p className="mt-1 font-mono text-[0.62rem] text-faint">
          +{formatCount(hiddenLanes)} more machines not shown (capped at {MAX_LANES} lanes)
        </p>
      )}

      {resolved && schedule.truncated && (
        <p className="mt-1.5 text-[0.68rem] text-warn">
          Showing {formatCount(schedule.previewCount)} of {formatCount(schedule.rowCount)} written
          operations — `result.preview` is downsampled server-side, so this is a sample of the
          durable schedule and not all of it. Machine utilisation is withheld for the same reason.
        </p>
      )}
      {resolved && layout.hidden > 0 && (
        <p className="mt-1.5 text-[0.68rem] text-warn">
          {formatCount(layout.hidden)} further operations were dropped by this chart's own bar cap.
        </p>
      )}
      {resolved && layout.spanFromMakespan && (
        <p className="mt-1.5 text-[0.68rem] text-faint">
          The axis runs to the run's recorded makespan ({formatCount(layout.span)} min), which is
          later than the last operation shown.
        </p>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */

function Header({
  copy,
  readout,
  phase,
  reducedMotion,
}: {
  copy: StateCopy;
  /** Real numbers off the wire. Monospace and right-aligned so it reads as
   *  telemetry rather than as caption. */
  readout?: string;
  /** Drives the ambient dot breath only. It says "the stream is live", which is
   *  true independently of anything the floor below is doing. Gated on the
   *  PHASE rather than on `isAnimating`, which is also true at QUEUED — where
   *  `motion.ts` is explicit that nothing may move. A queued run is told apart
   *  from no run at all by the lanes lifting, which costs no motion. */
  phase: MotionPhase;
  reducedMotion: boolean;
}) {
  const pulsing = (phase === "starting" || phase === "running") && !reducedMotion;
  return (
    <div className="mb-3.5 flex min-h-[2.6em] items-start gap-2.5">
      <motion.span
        className={[
          "mt-1 h-2.5 w-2.5 shrink-0 rounded-full border",
          copy.hollow ? "border-2 bg-transparent" : TONE_DOT[copy.tone],
          copy.hollow ? "" : "border-transparent",
          copy.hollow ? TONE_TEXT[copy.tone] : "",
        ].join(" ")}
        style={copy.hollow ? { borderColor: "currentColor" } : undefined}
        animate={pulsing ? { opacity: [1, 0.4, 1] } : { opacity: 1 }}
        // The same period as the floor's sweep, so the two ambient elements on
        // this panel breathe together instead of beating against each other.
        // Eased rather than linear: unlike the sweep, this one SHOULD pump —
        // that is what makes it a breath rather than a blink.
        transition={
          pulsing
            ? { duration: DURATION.ambient, ease: EASE.standard, repeat: Infinity }
            : { duration: 0 }
        }
        aria-hidden="true"
      />
      <div className="min-w-0">
        <div className={`text-[0.85rem] font-bold ${TONE_TEXT[copy.tone]}`}>{copy.title}</div>
        <div className="mt-0.5 text-[0.74rem] text-dim">{copy.detail}</div>
      </div>
      {readout !== undefined && (
        <span className="ml-auto self-center font-mono text-[0.7rem] whitespace-pre text-faint">
          {readout}
        </span>
      )}
    </div>
  );
}

function Lane({
  lane,
  state,
  phase,
  awake,
  index,
  laneCount,
  resolved,
  tone,
  decorative,
  pulse,
  horizon,
  reducedMotion,
  utilisation,
}: {
  lane: GanttLane;
  state: UiRunState | null;
  phase: MotionPhase;
  awake: boolean;
  index: number;
  laneCount: number;
  resolved: boolean;
  tone: StateCopy["tone"];
  decorative: readonly { x: number; width: number }[];
  pulse: number;
  /** The makespan edge, as a percentage of the lane's width. */
  horizon: number;
  reducedMotion: boolean;
  utilisation: number | null;
}) {
  // One flat terminal frame when there is nothing real to draw. INFEASIBLE
  // keeps its lanes visibly EMPTY behind a dashed warn edge — "no schedule
  // exists" — while FAILED washes them in the bad tone: the difference between
  // a known answer and no answer.
  const empty = !resolved && isSettled(state);
  const trackTone = empty
    ? state === "INFEASIBLE"
      ? "border-dashed border-warn bg-warn-soft"
      : `${TONE_SOFT[tone]} opacity-70`
    : "border-line bg-raised";

  // The lift out of idle is the only lane change that is an entrance rather
  // than a state swap, and it is what has to make a held STARTING frame look
  // different from no run at all — a cold job sits there for tens of seconds,
  // long after any motion has finished. `staggerFor` over the lane count keeps
  // the pass down the floor inside STAGGER.budget however many machines the
  // instance declared.
  const lift = reducedMotion
    ? { duration: 0 }
    : phase === "starting"
      ? {
          duration: DURATION.inhale,
          ease: EASE.decelerate,
          delay: index * staggerFor(laneCount),
        }
      : { duration: DURATION.base, ease: EASE.standard };

  // Every cell is placed EXPLICITLY rather than left to auto-flow. The horizon
  // overlay sits in the same column as the tracks and spans all their rows, and
  // auto-placement refuses to put an item in an occupied cell — it would have
  // pushed each lane's track into the utilisation column and walked the whole
  // floor one cell to the right. Explicitly placed items are allowed to
  // overlap, which is exactly what an overlay needs.
  const row = String(index + 1);

  return (
    <>
      <div
        className="truncate pr-1 font-mono text-[0.62rem] text-dim"
        style={{ gridColumn: "1", gridRow: row }}
        title={lane.label}
      >
        {lane.label}
      </div>
      <motion.div
        className={`relative h-[18px] overflow-hidden rounded-[3px] border ${trackTone}`}
        style={{ gridColumn: "2", gridRow: row }}
        // An `initial` only where the entrance is wanted. A view that mounts
        // straight into a terminal state must not fade its answer in: per
        // `contract.ts` a settled frame is flat from the first paint.
        initial={phase === "starting" && !reducedMotion ? { opacity: REST_OPACITY } : false}
        animate={{ opacity: awake ? 1 : REST_OPACITY }}
        transition={lift}
      >
        {resolved ? (
          lane.bars.map((bar) => (
            <div
              key={bar.key}
              className={`absolute inset-y-[2px] rounded-[2px] border ${TONE_FILL[tone]}`}
              style={{ left: percent(bar.x), width: percent(bar.width) }}
              title={`${bar.operation.jobLabel ?? `job ${bar.operation.jobId ?? "?"}`} — ${
                bar.operation.machineLabel ?? lane.label
              }, ${bar.operation.start}–${bar.operation.end} min (${bar.operation.duration} min)`}
            />
          ))
        ) : (
          decorative.length > 0 && (
            <motion.div
              // The floor COMPRESSES into the horizon rather than being clipped
              // by it: the same work fitting into less time is what a shrinking
              // makespan is. Nothing is seen to change its own duration,
              // because a fresh decorative layout arrives on the same event.
              className="absolute inset-y-0 left-0"
              initial={false}
              animate={{ width: `${horizon}%` }}
              transition={reducedMotion ? { duration: 0 } : SPRING.soft}
            >
              {decorative.map((bar) => (
                <motion.div
                  // Keying on the pulse is what makes an improvement redraw the
                  // lane rather than tween one arbitrary layout into another.
                  key={`${pulse}-${bar.x}`}
                  className="absolute inset-y-[2px] rounded-[2px] border border-info bg-info-soft"
                  style={{
                    left: percent(bar.x),
                    width: percent(bar.width),
                    // An operation starts at its start minute and extends
                    // forward; growing from the centre would run it backwards
                    // through time for half its entrance.
                    transformOrigin: "left",
                  }}
                  initial={reducedMotion ? false : { opacity: 0, scaleX: 0.35 }}
                  animate={{ opacity: 1, scaleX: 1 }}
                  transition={
                    reducedMotion
                      ? { duration: 0 }
                      : {
                          duration: DURATION.base,
                          ease: EASE.decelerate,
                          // A wave along TIME, not a per-item stagger: the bar
                          // count varies with the layout and runs into the
                          // hundreds, where `staggerFor` would return a step so
                          // small the whole group fires at once. Deriving the
                          // delay from the bar's own position spends the same
                          // STAGGER.budget and carries meaning — earlier
                          // operations settle first, which is the order a
                          // schedule is built in.
                          delay: (bar.x / PLOT_WIDTH) * STAGGER.budget,
                        }
                  }
                />
              ))}
            </motion.div>
          )
        )}
      </motion.div>
      <div
        className="text-right font-mono text-[0.6rem] text-faint"
        style={{ gridColumn: "3", gridRow: row }}
      >
        {utilisation === null ? "" : `${utilisation}%`}
      </div>
    </>
  );
}

/**
 * One pass of light across the horizon, left to right.
 *
 * The same arrangement `gurobi_scheduling` and `gurobi_routing` use, because
 * `motion.ts` asks every model to express a phase the same way: the STARTING
 * inhale runs it once over DURATION.inhale, RUNNING loops it at
 * DURATION.ambient. Left to right because that is the time axis of the floor
 * it crosses, so the sweep is the solver working forward through the horizon
 * rather than a decoration that happens to move.
 *
 * The gradient is asymmetric on purpose. A bright leading edge with the fade
 * behind it reads as a front advancing; the symmetric band is the skeleton
 * shimmer every loading screen in the world uses, and this is not a loading
 * state — it is a search that may run for ten minutes.
 *
 * `opacity` rides the same keyframe list as `x` rather than getting its own
 * `repeat: Infinity` transition. Two independently scheduled loops of nominally
 * equal length drift, and a band whose fade has slipped out of step with its
 * travel reads as a glitch. Sharing the list also puts the band at zero opacity
 * at both ends of the travel, so the loop's restart is never seen.
 */
function SearchSweep({ tone, loop }: { tone: Tone; loop: boolean }) {
  return (
    <motion.div
      // `currentColor` rather than a value: the tone classes are bound to the
      // CSS tokens, which the dark palette re-points at runtime.
      className={TONE_TEXT[tone]}
      style={{
        position: "absolute",
        top: 0,
        bottom: 0,
        left: `-${SWEEP_PCT}%`,
        width: `${SWEEP_PCT}%`,
        background: "linear-gradient(90deg, transparent, currentColor)",
      }}
      initial={{ x: "0%", opacity: 0 }}
      animate={{
        // Evenly spaced against `times`, so the travel stays linear while the
        // opacity gets its ramps.
        x: ["0%", `${SWEEP_TRAVEL * 0.15}%`, `${SWEEP_TRAVEL * 0.85}%`, `${SWEEP_TRAVEL}%`],
        opacity: [0, SWEEP_PEAK, SWEEP_PEAK, 0],
      }}
      transition={{
        duration: loop ? DURATION.ambient : DURATION.inhale,
        times: [0, 0.15, 0.85, 1],
        // Linear, and the only linear thing in this file. A loop that eases
        // pumps once a cycle, which over a long solve is precisely the thing
        // that becomes irritating.
        ease: "linear",
        ...(loop ? { repeat: Infinity } : {}),
      }}
    />
  );
}

/**
 * `percent_complete`, labelled as what it is.
 *
 * This is the one real number on the whole signature while a run is live, and
 * the one most easily misread. It is elapsed solver time against
 * `max_time_in_seconds`, which the model states in the record itself
 * (`percent_complete_basis`) because the envelope has no label field for it.
 * An unrecognised basis is printed verbatim rather than described, so a future
 * change to the model's denominator cannot leave this component confidently
 * saying the wrong thing.
 */
function SolverClockBar({
  clock,
  animating,
  reducedMotion,
}: {
  clock: { percent: number | null; basis: string | null };
  animating: boolean;
  reducedMotion: boolean;
}) {
  if (clock.percent === null) {
    if (!animating) return null;
    return (
      <p className="mt-2.5 text-[0.68rem] text-faint">
        No time limit configured, so there is no honest fraction to report while the search runs.
      </p>
    );
  }

  const value = Math.max(0, Math.min(100, clock.percent));
  const caption =
    clock.basis === null || clock.basis === "elapsed_solver_time_against_time_limit"
      ? "elapsed solver time against the time limit — a clock, not search progress"
      : `basis: ${clock.basis}`;

  return (
    <div className="mt-2.5">
      <div className="h-1.5 overflow-hidden rounded-full bg-line">
        <div
          className="h-full rounded-full bg-accent"
          style={{
            width: `${value}%`,
            // Inline rather than the `motion-reduce:` variant: an inline
            // `transitionProperty` outranks a class, so the class would
            // silently lose this argument. The width carries a real number, so
            // reduced motion drops the tween and keeps the value.
            transitionProperty: reducedMotion ? "none" : "width",
            transitionDuration: `${DURATION.base}s`,
            transitionTimingFunction: `cubic-bezier(${EASE.standard.join(",")})`,
          }}
        />
      </div>
      <p className="mt-1 font-mono text-[0.62rem] text-faint">
        clock {value.toFixed(0)}% — {caption}
      </p>
    </div>
  );
}
