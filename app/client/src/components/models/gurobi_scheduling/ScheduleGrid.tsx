/**
 * The signature: a staff x day schedule grid.
 *
 * ## What the board is keyed to
 *
 * `contract.ts`: a signature is a state machine over the run lifecycle, not a
 * rendering of live numbers. The hook here is the incumbent count.
 * `job/drivers/gurobi.py` samples `solution_count` about every two seconds, and
 * a strict increase is the one observable event meaning "the solver accepted a
 * new, better, feasible schedule". The cells re-lay on exactly those events and
 * on nothing else — no timer touches them anywhere in this file.
 *
 * WHICH cells light is decorative and cannot be otherwise: no progress message
 * carries per-cell or candidate-schedule data. `decorativeCells` is pure and
 * deterministic, so two tabs watching one run draw the same board.
 *
 * ## The phases of `motion.ts`, as frames
 *
 *   idle       the board dimmed and empty. Nothing has been asked for.
 *   starting   the board lifts to full in one left-to-right pass over
 *              DURATION.inhale while a single sweep of light crosses it, then
 *              HOLDS. A cold job sits in this phase for tens of seconds, so the
 *              held frame has to be visibly different from idle rather than
 *              relying on motion that has already finished.
 *   running    two sub-states, split on a real event rather than on a clock.
 *              Before the first incumbent the board stays EMPTY and only the
 *              sweep loops at DURATION.ambient — the search working across the
 *              horizon, which is exactly what the RUNNING copy claims. After
 *              it, cells carry the candidate wash and re-lay in a left-to-right
 *              wave once per incumbent, with the sweep continuing between them.
 *   settled    one flat frame. The real schedule when the run wrote rows, the
 *              terminal tone otherwise. Nothing loops, drifts or pulses.
 *
 * Three things the previous version got wrong, all of them easy to miss:
 *
 *  - It lit cells at QUEUED and before the first incumbent. `decorativeCells(0,
 *    …)` is not an empty board — it lights roughly a fifth of it — so a run
 *    still waiting for one of the five job slots drew a half-built schedule,
 *    with the header saying "no feasible schedule found yet" over the top of
 *    it. Cells now require `pulses > 0`.
 *  - Nothing moved during RUNNING between incumbents, and incumbents can be
 *    minutes apart or absent entirely: a long presolve emits no progress
 *    messages at all. A live solve was pixel-identical to a dead page.
 *  - The reveal stagger was `(flat % 48) * 6ms`, a row-major ripple restarting
 *    every 48 cells for no reason anyone could state. It is a column wave
 *    inside `STAGGER.budget` now, which also agrees with the sweep's direction.
 *
 * ## Why the cells are not `motion` components
 *
 * Ten rows by twenty-one columns is 210 elements, and this component re-renders
 * on every log line of a chatty run. The cells only ever do discrete state
 * swaps — no keyframes, no springs, nothing continuous — which a CSS transition
 * does on the compositor for the cost of a style object. The one thing that
 * genuinely animates over time is the sweep, and that is the one `motion`
 * element here.
 *
 * The price of the split is that a cell has no `initial`, so the lift does not
 * replay for a view that mounts straight into STARTING (which is the common
 * case — triggering a run navigates here). The sweep does still play, and that
 * is the half of the inhale worth having; paying for the other half with 210
 * `motion` components on a page meant to sit open for ten minutes is not a
 * trade worth making.
 */

import { motion } from "motion/react";
import type { CSSProperties } from "react";

import type { UiRunState } from "@/lib/envelope";
import { formatCount, formatMetric } from "@/lib/format";
import { isAnimating, isSettled, type ModelViewProps } from "../contract";
import { DURATION, EASE, phaseOf, staggerFor } from "../motion";
import { usePrefersReducedMotion } from "../useReducedMotion";
import { deriveMipSeries, incumbentActivity, terminalDetail } from "../gurobi_shared/mipSeries";
import { SignatureHeader } from "../gurobi_shared/SignatureHeader";
import { TONE_FILL, TONE_SOFT, TONE_TEXT, type Tone } from "../gurobi_shared/tones";
import { describeSchedulingState } from "./stateCopy";
import {
  assignmentIndex,
  decorativeCells,
  parseCoverage,
  parseScheduleShape,
  resolveSchedule,
  SHIFT_ORDER,
  type CellHeat,
} from "./schedule";

/** Beyond this the grid stops being readable at 20px cells and starts being a
 *  texture. The rest are reported as a count, never silently dropped. */
const MAX_ROWS = 10;
const MAX_COLS = 21;

/** The board's geometry, in one place. The grid template and the sweep's clip
 *  box are both derived from these, so the light cannot drift off the cells. */
const CELL_PX = 20;
const GAP_PX = 3;
const LABEL_PX = 72;

/** Shift intensity inside one tone, so the three shifts are distinguishable
 *  without spending three unrelated semantic colours on them. */
const SHIFT_OPACITY = [0.45, 0.72, 1] as const;

/** The board before a run exists. Low enough to read as "not asked yet", high
 *  enough that the instance's dimensions are still legible. */
const REST_OPACITY = 0.4;

/**
 * Weight of the three live cell states.
 *
 * `seed` is `decorativeCells`' stable wash and sits back deliberately: a pulse
 * has to read as a redistribution of effort against a background rather than as
 * the whole board changing its mind.
 */
const HEAT_OPACITY: Record<CellHeat, number> = { seed: 0.8, cool: 0.6, hot: 1 };

/** Sweep width, in cells, and its peak opacity. Four cells is wide enough to
 *  read as a front rather than a cursor; the opacity is low because this is the
 *  one element on screen that repeats forever, and anything with real presence
 *  at that cadence becomes the thing you cannot stop looking at. */
const SWEEP_PX = 4 * (CELL_PX + GAP_PX);
const SWEEP_PEAK = 0.16;

export function ScheduleGrid({ state, snapshot }: ModelViewProps) {
  const reduced = usePrefersReducedMotion();
  const phase = phaseOf(state);

  const shape = parseScheduleShape(snapshot.logs);
  const coverage = parseCoverage(snapshot.logs);
  const schedule = resolveSchedule(snapshot.results);
  const activity = incumbentActivity(deriveMipSeries(snapshot.progress));

  const copy = describeSchedulingState(state, {
    schedule,
    solutionCount: activity.solutionCount,
    detail: terminalDetail(snapshot.statuses),
    clippedDemand: coverage?.clipped === true,
  });

  // The grid resolves to real data whenever real data exists — which includes
  // a cancelled run, because `results()` is never gated on OPTIMAL and a
  // cancelled run keeps its incumbent.
  const resolved = isSettled(state) && schedule.assignments.length > 0;

  /** A run exists at all. Deliberately not `phase !== "idle"`: `phaseOf` calls
   *  QUEUED idle, correctly, because nothing may move there — but dimming the
   *  board back down between STARTING and RUNNING would read as the run being
   *  un-asked for a second. The lift is a fact about whether a run exists; the
   *  phase decides what, if anything, moves. */
  const awake = state !== null;

  const staffLabels = resolved
    ? schedule.staff
    : Array.from({ length: shape.staffCount }, (_, i) => `staff-${String(i).padStart(2, "0")}`);
  const dayCount = resolved ? Math.max(schedule.days.length, 1) : shape.days;

  const rows = Math.min(staffLabels.length, MAX_ROWS);
  const cols = Math.min(dayCount, MAX_COLS);
  const hiddenStaff = staffLabels.length - rows;
  const hiddenDays = dayCount - cols;

  // Candidates exist on screen only once a feasible schedule exists in fact.
  // Before the first incumbent — which on a hard instance is the whole run —
  // the board stays empty, and INFEASIBLE and FAILED therefore have no
  // per-cell meaning left over for their terminal frame to keep.
  const cells =
    phase === "running" && activity.pulses > 0 ? decorativeCells(activity.pulses, rows, cols) : null;
  const index = resolved ? assignmentIndex(schedule) : null;
  const shifts = schedule.shifts.length > 0 ? schedule.shifts : [...SHIFT_ORDER];

  // One transition per COLUMN, not per cell: the wave runs along the day axis,
  // so every cell in a column shares a delay and there are at most 21 of these.
  const columnStep = reduced ? 0 : staggerFor(cols);
  // The only cell change that is an entrance rather than a state swap is the
  // one leaving idle, and it is also the one that has to fill a cold start's
  // worth of dead air.
  const cellSeconds = phase === "starting" ? DURATION.inhale : DURATION.base;
  const cellEase: readonly number[] = phase === "starting" ? EASE.decelerate : EASE.standard;
  const columnTransition: CSSProperties[] = Array.from({ length: cols }, (_, day) =>
    reduced
      ? // Not the `motion-reduce:` variant: an inline `transitionProperty`
        // outranks a class, so the class would silently lose this argument.
        { transitionProperty: "none" }
      : {
          transitionProperty: "background-color, border-color, opacity, transform",
          transitionDuration: `${cellSeconds}s`,
          transitionTimingFunction: `cubic-bezier(${cellEase.join(", ")})`,
          transitionDelay: `${(day * columnStep).toFixed(3)}s`,
        },
  );

  const board = {
    left: LABEL_PX + GAP_PX,
    top: CELL_PX + GAP_PX,
    width: cols * CELL_PX + Math.max(cols - 1, 0) * GAP_PX,
    height: rows * CELL_PX + Math.max(rows - 1, 0) * GAP_PX,
  };

  const readout = [
    activity.solutionCount === null ? null : `solutions ${formatCount(activity.solutionCount)}`,
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

      <div className="overflow-x-auto pb-1">
        <div
          className="relative inline-grid items-center"
          style={{
            gridTemplateColumns: `${LABEL_PX}px repeat(${cols}, ${CELL_PX}px)`,
            gridAutoRows: `${CELL_PX}px`,
            gap: `${GAP_PX}px`,
          }}
          role="img"
          aria-label={`${copy.title}. ${copy.detail}`}
        >
          <div />
          {Array.from({ length: cols }, (_, day) => (
            <div
              key={`col-${day}`}
              className="flex items-end justify-center pb-0.5 font-mono text-[0.6rem] text-faint"
            >
              {day === 0 || day === cols - 1 || day === Math.floor(cols / 2) ? `D${day + 1}` : ""}
            </div>
          ))}

          {Array.from({ length: rows }, (_, row) => {
            const staff = staffLabels[row] ?? `staff-${row}`;
            return [
              <div
                key={`row-${row}`}
                className="flex items-center truncate pr-2 font-mono text-[0.62rem] text-dim"
              >
                {staff}
              </div>,
              ...Array.from({ length: cols }, (_, day) => (
                <Cell
                  key={`cell-${row}-${day}`}
                  tone={copy.tone}
                  resolved={resolved}
                  awake={awake}
                  heat={cells?.get(row * cols + day)}
                  assignment={index?.get(`${staff}|${day}`)?.shift}
                  shifts={shifts}
                  state={state}
                  transition={columnTransition[day]}
                />
              )),
            ];
          })}

          {/* Last in the DOM so it passes OVER the cells, and dropped entirely
              under reduced motion: it is purely ambient and states nothing the
              header does not already say in words. */}
          {!reduced && (phase === "starting" || phase === "running") && (
            <SearchSweep key={phase} {...board} tone={copy.tone} loop={phase === "running"} />
          )}
        </div>
      </div>

      {(hiddenStaff > 0 || hiddenDays > 0) && (
        <p className="pt-1.5 font-mono text-[0.62rem] text-faint">
          {[
            hiddenStaff > 0 ? `+${hiddenStaff} more staff` : null,
            hiddenDays > 0 ? `+${hiddenDays} more days` : null,
          ]
            .filter((part) => part !== null)
            .join(" · ")}{" "}
          not shown (grid is capped at {MAX_ROWS}x{MAX_COLS})
        </p>
      )}

      {resolved && (
        <div className="mt-2.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[0.68rem] text-dim">
          {shifts.map((shift, i) => (
            <span key={shift} className="inline-flex items-center gap-1.5">
              <span
                className={`inline-block h-3 w-3 rounded-[3px] border ${TONE_FILL[copy.tone]}`}
                style={{ opacity: SHIFT_OPACITY[Math.min(i, SHIFT_OPACITY.length - 1)] }}
              />
              {shift}
            </span>
          ))}
          <span className="text-faint">
            {formatCount(schedule.assignments.filter((a) => a.preferred).length)} of{" "}
            {formatCount(schedule.assignments.length)} shown shifts are a preferred pairing
          </span>
        </div>
      )}

      {resolved && schedule.truncated && (
        <p className="mt-1.5 text-[0.68rem] text-warn">
          Showing {formatCount(schedule.previewCount)} of {formatCount(schedule.rowCount)} written
          rows — `result.preview` is downsampled server-side, so this grid is a sample of the
          durable schedule, not all of it.
        </p>
      )}

      {!resolved && shape.source === "default" && (
        <p className="mt-2 text-[0.68rem] text-faint">
          Grid dimensions are the model's defaults — this run has not logged its instance size yet.
        </p>
      )}
      {!resolved && shape.source === "log" && coverage !== null && (
        <p className="mt-2 text-[0.68rem] text-faint">
          {formatCount(coverage.totalDemand)} staff-shifts of coverage required over{" "}
          {formatCount(coverage.days)} days, from {coverage.derivedFrom}
          {coverage.clipped ? " (clipped to workforce capacity)" : ""}.
        </p>
      )}
    </div>
  );
}

/**
 * One pass of light across the board, left to right.
 *
 * Two callers, one shape — the same arrangement `gurobi_routing` uses for its
 * depot wave, so the two Gurobi views express the same phase the same way: the
 * STARTING inhale runs it once over DURATION.inhale, RUNNING loops it at
 * DURATION.ambient. Left to right because `decorativeCells` already biases
 * activity rightwards (later days depend on earlier ones through the rest
 * constraint), so the two decorative layers at least agree on a direction.
 *
 * The gradient is asymmetric on purpose. A bright leading edge with the fade
 * behind it reads as a front moving across the schedule; the symmetric band is
 * the skeleton shimmer every loading screen in the world uses, and this is not
 * a loading state — it is a solve that may run for ten minutes.
 *
 * `opacity` rides the same keyframe list as `x` rather than getting its own
 * `repeat: Infinity` transition. Two independently scheduled loops of nominally
 * equal length drift, and a band whose fade has slipped out of step with its
 * travel reads as a glitch. Sharing the list also puts the band at zero opacity
 * at both ends of the travel, so the loop's restart is never seen.
 */
function SearchSweep({
  left,
  top,
  width,
  height,
  tone,
  loop,
}: {
  left: number;
  top: number;
  width: number;
  height: number;
  tone: Tone;
  /** Ambient and endless, versus the single inhale that says "asked". */
  loop: boolean;
}) {
  const travel = width + SWEEP_PX;
  return (
    <div
      className="pointer-events-none absolute overflow-hidden"
      style={{ left, top, width, height }}
      aria-hidden="true"
    >
      <motion.div
        // `currentColor` rather than a value: the tone classes are bound to the
        // CSS tokens, which the dark palette re-points at runtime.
        className={TONE_TEXT[tone]}
        style={{
          position: "absolute",
          left: 0,
          insetBlock: 0,
          width: SWEEP_PX,
          background: "linear-gradient(90deg, transparent, currentColor)",
        }}
        initial={{ x: -SWEEP_PX, opacity: 0 }}
        animate={{
          // Evenly spaced against `times`, so the travel stays linear while the
          // opacity gets its ramps.
          x: [-SWEEP_PX, -SWEEP_PX + travel * 0.15, -SWEEP_PX + travel * 0.85, width],
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
    </div>
  );
}

/** The three live weights, tone-driven rather than a hardcoded `info` map: the
 *  header's tone and the board's colour must not be able to disagree. */
function heatClass(tone: Tone, heat: CellHeat): string {
  if (heat === "hot") return `${TONE_FILL[tone]} scale-110`;
  if (heat === "cool") return `${TONE_SOFT[tone]} scale-95`;
  return TONE_SOFT[tone];
}

function Cell({
  tone,
  resolved,
  awake,
  heat,
  assignment,
  shifts,
  state,
  transition,
}: {
  tone: Tone;
  resolved: boolean;
  /** A run exists — the board is lifted out of its resting dim. */
  awake: boolean;
  heat: CellHeat | undefined;
  assignment: string | undefined;
  shifts: readonly string[];
  state: UiRunState | null;
  /** This column's share of the left-to-right wave. Shared by reference across
   *  the column's cells. */
  transition: CSSProperties | undefined;
}) {
  const base = "rounded-[3px] border motion-reduce:transform-none";
  const box: CSSProperties = { width: CELL_PX, height: CELL_PX, ...transition };

  if (resolved) {
    if (assignment === undefined) {
      return <div className={`${base} border-line bg-paper`} style={box} />;
    }
    const shiftIndex = Math.max(0, shifts.indexOf(assignment));
    return (
      <div
        className={`${base} ${TONE_FILL[tone]}`}
        style={{ ...box, opacity: SHIFT_OPACITY[Math.min(shiftIndex, SHIFT_OPACITY.length - 1)] }}
        title={`${assignment} shift`}
      />
    );
  }

  // One flat terminal frame. INFEASIBLE keeps its cells EMPTY behind a dashed
  // warn border — "no assignment exists" — while the rest wash the whole grid
  // in the terminal tone: the difference between a known answer and no answer.
  if (state === "INFEASIBLE") {
    return <div className={`${base} border-dashed border-warn bg-warn-soft`} style={box} />;
  }
  if (isSettled(state)) {
    return <div className={`${base} ${TONE_SOFT[tone]}`} style={{ ...box, opacity: 0.7 }} />;
  }

  if (heat === undefined) {
    return (
      <div
        className={`${base} border-line bg-paper`}
        style={{ ...box, opacity: awake ? 1 : REST_OPACITY }}
      />
    );
  }
  return (
    <div
      className={`${base} ${heatClass(tone, heat)}`}
      style={{ ...box, opacity: HEAT_OPACITY[heat] }}
    />
  );
}
