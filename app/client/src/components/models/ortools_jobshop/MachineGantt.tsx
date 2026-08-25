/**
 * The signature: a machine-by-machine Gantt of the shop floor.
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
 * ## The state machine, one sentence per state
 *
 *  - no run / STARTING / QUEUED: empty lanes at the instance's machine count.
 *  - RUNNING: bars are DECORATIVE. No progress message carries a single
 *    operation's start time — `model.py::_read_solution` reads the schedule
 *    out of the solver ONCE, after `solve()` returns — so there is nothing
 *    real to draw. WHEN the floor redraws is real: once per observed
 *    improvement in `solutions_found`, and no timer in this file.
 *  - SUCCEEDED / CANCELLED with rows: the real schedule from `result.preview`.
 *  - INFEASIBLE: one flat state, and visually distinct from FAILED. The lanes
 *    stay empty behind a dashed warn edge — "no schedule exists inside your
 *    deadline" — because the solver correctly answered a question about the
 *    input rather than crashing.
 *  - FAILED: one flat, washed-out state; the floor's contents are unknown,
 *    not empty.
 *
 * ## Where this reads differently from the two Gurobi views
 *
 * Two things, and both are deliberately made legible rather than smoothed
 * over. A MIP callback fires constantly, so a still Gurobi grid is ambiguous;
 * CP-SAT's solution callback fires only on an improving solution, two to
 * twenty times across a whole run, so a still floor here genuinely means the
 * search is not improving. And `percent_complete` is populated on this model
 * where it is permanently null on those two — which makes it more dangerous,
 * not less, because it is a TIME fraction. The clock bar below the lanes says
 * so on its own line rather than leaving the honesty note to carry it alone.
 */

import { motion } from "motion/react";

import type { UiRunState } from "@/lib/envelope";
import { formatCount, formatMetric } from "@/lib/format";
import { isAnimating, isSettled, type ModelViewProps } from "../contract";
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
import { TONE_DOT, TONE_FILL, TONE_SOFT, TONE_TEXT, type StateCopy } from "./tone";

/** Enough lanes for the five stages plus headroom for an instance that grows
 *  one. Past this the floor stops being readable and the rest are counted. */
const MAX_LANES = 12;

const percent = (units: number) => `${(units / PLOT_WIDTH) * 100}%`;

export function MachineGantt({ state, snapshot }: ModelViewProps) {
  const reduced = usePrefersReducedMotion();

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
  // Only while the search is actually running. QUEUED and STARTING have not
  // begun the thing a busy floor would depict, and "no run selected" showing
  // occupied machines would be a picture of nothing at all.
  const decorative =
    state === "RUNNING" ? decorativeBars(activity.improvements, lanes.length) : [];
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
        animating={isAnimating(state)}
        reducedMotion={reduced}
      />

      <div
        className="rounded-lg border border-line bg-paper p-2.5"
        role="img"
        aria-label={`${copy.title}. ${copy.detail}`}
      >
        <div className="grid grid-cols-[68px_1fr_2.4rem] items-center gap-x-2 gap-y-1">
          {lanes.map((lane) => (
            <Lane
              key={lane.machineId}
              lane={lane}
              state={state}
              resolved={resolved}
              tone={copy.tone}
              decorative={decorative.filter((bar) => bar.lane === lane.machineId)}
              pulse={activity.improvements}
              reducedMotion={reduced}
              utilisation={
                showUtilisation ? Math.round((lane.busyMinutes / layout.span) * 100) : null
              }
            />
          ))}
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

      <SolverClockBar clock={clock} animating={isAnimating(state)} />

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
  animating,
  reducedMotion,
}: {
  copy: StateCopy;
  /** Real numbers off the wire. Monospace and right-aligned so it reads as
   *  telemetry rather than as caption. */
  readout?: string;
  /** Drives the ambient dot pulse only. It says "the stream is live", which is
   *  true independently of anything the floor below is doing. */
  animating: boolean;
  reducedMotion: boolean;
}) {
  const pulsing = animating && !reducedMotion;
  return (
    <div className="mb-3.5 flex min-h-[2.6em] items-start gap-2.5">
      <span
        className={[
          "mt-1 h-2.5 w-2.5 shrink-0 rounded-full border",
          copy.hollow ? "border-2 bg-transparent" : TONE_DOT[copy.tone],
          copy.hollow ? "" : "border-transparent",
          pulsing ? "animate-pulse" : "",
          copy.hollow ? TONE_TEXT[copy.tone] : "",
        ].join(" ")}
        style={copy.hollow ? { borderColor: "currentColor" } : undefined}
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
  resolved,
  tone,
  decorative,
  pulse,
  reducedMotion,
  utilisation,
}: {
  lane: GanttLane;
  state: UiRunState | null;
  resolved: boolean;
  tone: StateCopy["tone"];
  decorative: readonly { x: number; width: number }[];
  pulse: number;
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

  return (
    <>
      <div className="truncate pr-1 font-mono text-[0.62rem] text-dim" title={lane.label}>
        {lane.label}
      </div>
      <div className={`relative h-[18px] overflow-hidden rounded-[3px] border ${trackTone}`}>
        {resolved
          ? lane.bars.map((bar) => (
              <div
                key={bar.key}
                className={`absolute inset-y-[2px] rounded-[2px] border ${TONE_FILL[tone]}`}
                style={{ left: percent(bar.x), width: percent(bar.width) }}
                title={`${bar.operation.jobLabel ?? `job ${bar.operation.jobId ?? "?"}`} — ${
                  bar.operation.machineLabel ?? lane.label
                }, ${bar.operation.start}–${bar.operation.end} min (${bar.operation.duration} min)`}
              />
            ))
          : !empty &&
            decorative.map((bar) => (
              <motion.div
                // Keying on the pulse is what makes an improvement redraw the
                // lane rather than tween one arbitrary layout into another.
                key={`${pulse}-${bar.x}`}
                className="absolute inset-y-[2px] rounded-[2px] border border-info bg-info-soft"
                style={{ left: percent(bar.x), width: percent(bar.width) }}
                initial={reducedMotion ? false : { opacity: 0.15, scaleX: 0.6 }}
                animate={{ opacity: 1, scaleX: 1 }}
                transition={reducedMotion ? { duration: 0 } : { duration: 0.4, ease: "easeOut" }}
              />
            ))}
      </div>
      <div className="text-right font-mono text-[0.6rem] text-faint">
        {utilisation === null ? "" : `${utilisation}%`}
      </div>
    </>
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
}: {
  clock: { percent: number | null; basis: string | null };
  animating: boolean;
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
          className="h-full rounded-full bg-accent transition-[width] duration-500 motion-reduce:transition-none"
          style={{ width: `${value}%` }}
        />
      </div>
      <p className="mt-1 font-mono text-[0.62rem] text-faint">
        clock {value.toFixed(0)}% — {caption}
      </p>
    </div>
  );
}
