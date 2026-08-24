/**
 * The signature: a staff x day schedule grid.
 *
 * The state machine, in one sentence per state — because this component is a
 * state machine keyed to the run lifecycle, not a rendering of live numbers:
 *
 *  - no run / QUEUED / STARTING: an empty grid at the run's dimensions.
 *  - RUNNING: cells flicker. Which cells is DECORATIVE (no progress message
 *    carries per-cell or candidate-schedule data). WHEN they change is real:
 *    the frame index is the count of observed `solution_count` increments, so
 *    the grid steps exactly once per new incumbent and sits still when the
 *    solver is not improving. There is no timer in this file.
 *  - SUCCEEDED / CANCELLED with rows: the grid resolves into the REAL
 *    schedule from `result.preview`. This is why this model needs no separate
 *    results disclosure — its terminal frame is the results view.
 *  - INFEASIBLE: one flat state, visually distinct from FAILED. An infeasible
 *    model is the solver correctly answering that the request is impossible;
 *    it is not a crash, and colouring it like one teaches the wrong reflex.
 *  - FAILED: one flat, greyed-out state — the grid's contents are unknown,
 *    not empty.
 */

import { formatCount, formatMetric } from "@/lib/format";
import type { ModelViewProps } from "../contract";
import { usePrefersReducedMotion } from "../useReducedMotion";
import { deriveMipSeries, incumbentActivity, terminalDetail } from "../gurobi_shared/mipSeries";
import {
  assignmentIndex,
  decorativeCells,
  parseCoverage,
  parseScheduleShape,
  resolveSchedule,
  SHIFT_ORDER,
  type CellHeat,
} from "./schedule";
import { SignatureHeader } from "../gurobi_shared/SignatureHeader";
import { describeSchedulingState } from "./stateCopy";
import { TONE_FILL, TONE_SOFT, type Tone } from "../gurobi_shared/tones";
import { isAnimating, isSettled } from "../contract";
import type { UiRunState } from "@/lib/envelope";

/** Beyond this the grid stops being readable at 20px cells and starts being a
 *  texture. The rest are reported as a count, never silently dropped. */
const MAX_ROWS = 10;
const MAX_COLS = 21;

const CELL_LIVE: Record<CellHeat, string> = {
  seed: "bg-info-soft border-info",
  cool: "bg-info-soft border-info scale-95",
  hot: "bg-info border-info scale-110",
};

/** Shift intensity inside one tone, so the three shifts are distinguishable
 *  without spending three unrelated semantic colours on them. */
const SHIFT_OPACITY = [0.45, 0.72, 1] as const;

export function ScheduleGrid({ state, snapshot }: ModelViewProps) {
  const reduced = usePrefersReducedMotion();

  const shape = parseScheduleShape(snapshot.logs);
  const coverage = parseCoverage(snapshot.logs);
  const schedule = resolveSchedule(snapshot.results);
  const activity = incumbentActivity(deriveMipSeries(snapshot.progress));
  const settled = isSettled(state);

  const copy = describeSchedulingState(state, {
    schedule,
    solutionCount: activity.solutionCount,
    detail: terminalDetail(snapshot.statuses),
    clippedDemand: coverage?.clipped === true,
  });

  // The grid resolves to real data whenever real data exists — which includes
  // a cancelled run, because `results()` is never gated on OPTIMAL and a
  // cancelled run keeps its incumbent.
  const resolved = settled && schedule.assignments.length > 0;

  const staffLabels = resolved
    ? schedule.staff
    : Array.from({ length: shape.staffCount }, (_, i) => `staff-${String(i).padStart(2, "0")}`);
  const dayCount = resolved ? Math.max(schedule.days.length, 1) : shape.days;

  const rows = Math.min(staffLabels.length, MAX_ROWS);
  const cols = Math.min(dayCount, MAX_COLS);
  const hiddenStaff = staffLabels.length - rows;
  const hiddenDays = dayCount - cols;

  const cells = resolved ? null : decorativeCells(activity.pulses, rows, cols);
  const index = resolved ? assignmentIndex(schedule) : null;
  const shifts = schedule.shifts.length > 0 ? schedule.shifts : [...SHIFT_ORDER];

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
          className="inline-grid items-center gap-[3px]"
          style={{ gridTemplateColumns: `72px repeat(${cols}, 20px)`, gridAutoRows: "20px" }}
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
              ...Array.from({ length: cols }, (_, day) => {
                const flat = row * cols + day;
                return (
                  <Cell
                    key={`cell-${row}-${day}`}
                    tone={copy.tone}
                    resolved={resolved}
                    heat={cells?.get(flat)}
                    assignment={index?.get(`${staff}|${day}`)?.shift}
                    shifts={shifts}
                    state={state}
                    delayMs={reduced ? 0 : (flat % 48) * 6}
                  />
                );
              }),
            ];
          })}
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

function Cell({
  tone,
  resolved,
  heat,
  assignment,
  shifts,
  state,
  delayMs,
}: {
  tone: Tone;
  resolved: boolean;
  heat: CellHeat | undefined;
  assignment: string | undefined;
  shifts: readonly string[];
  state: UiRunState | null;
  delayMs: number;
}) {
  const base =
    "h-5 w-5 rounded-[3px] border transition-[background-color,border-color,transform] duration-300 motion-reduce:transition-none motion-reduce:transform-none";

  if (resolved) {
    if (assignment === undefined) {
      return <div className={`${base} border-line bg-paper`} style={{ transitionDelay: `${delayMs}ms` }} />;
    }
    const shiftIndex = Math.max(0, shifts.indexOf(assignment));
    return (
      <div
        className={`${base} ${TONE_FILL[tone]}`}
        style={{
          opacity: SHIFT_OPACITY[Math.min(shiftIndex, SHIFT_OPACITY.length - 1)],
          transitionDelay: `${delayMs}ms`,
        }}
        title={`${assignment} shift`}
      />
    );
  }

  // One flat terminal frame. INFEASIBLE keeps its cells EMPTY behind a dashed
  // warn border — "no assignment exists" — while FAILED washes the whole grid
  // in the bad tone: the difference between a known answer and no answer.
  if (state === "INFEASIBLE") {
    return <div className={`${base} border-dashed border-warn bg-warn-soft`} />;
  }
  if (state === "FAILED" || state === "CANCELLED" || state === "SUCCEEDED") {
    return <div className={`${base} ${TONE_SOFT[tone]} opacity-70`} />;
  }

  if (heat === undefined) {
    return <div className={`${base} border-line bg-paper`} style={{ transitionDelay: `${delayMs}ms` }} />;
  }
  return (
    <div
      className={`${base} ${CELL_LIVE[heat]}`}
      style={{ transitionDelay: `${delayMs}ms` }}
    />
  );
}
