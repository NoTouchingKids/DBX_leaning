/**
 * The two diagnostics charts. Every value drawn here came off the wire in a
 * `progress` message — which is what earns the signature next to them the
 * right to be decorative.
 *
 * ## Duplicated from `gurobi_shared/GurobiCharts.tsx`, not reused
 *
 * The convergence chart is visually the same idea, and reusing it was the
 * obvious move. It is the wrong one, for three separate reasons:
 *
 *  1. **The gap is not `mip_gap`.** `model.py::_emit_progress` sets
 *     `primary_metric_label` to `relative_gap` deliberately — same formula as
 *     `job/drivers/gurobi.py`, but this is a CP-SAT search and not a MIP, and
 *     the whole reason this model is in the lineup is that contrast. A legend
 *     reading "mip_gap" on a CP-SAT chart undoes the point of the model.
 *  2. **The second chart has no shared fields at all.** Gurobi reports
 *     `nodes_explored` / `nodes_remaining` / `solution_count`; CP-SAT reports
 *     `solutions_found`, `conflicts` and `branches`. Pointing the shared
 *     derivation at this payload would read four absent keys and render an
 *     empty chart under confident axis labels.
 *  3. **`gurobi_shared/` says it is for the two Gurobi views and nothing
 *     else.** A third model reaching into it makes it a de-facto common
 *     module that nobody declared, and the next edit to the Gurobi charts
 *     would silently change this page.
 *
 * The objective is a makespan in MINUTES here, not an abstract cost, so the
 * axis says so.
 */

import {
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ResponsiveContainer,
  Scatter,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { formatDuration } from "@/lib/format";
import type { ModelViewProps } from "../contract";
import { deriveJobshopSeries, hasPlottable, type JobshopPoint } from "./series";

const AXIS = { fill: "var(--c-faint)", fontSize: 10 } as const;
const TOOLTIP_STYLE = {
  background: "var(--c-raised)",
  border: "1px solid var(--c-edge)",
  borderRadius: 8,
  fontSize: "0.72rem",
  color: "var(--c-ink)",
} as const;

/**
 * The state a chart is in before the first improving solution, and the only
 * state an INFEASIBLE run's charts ever reach.
 *
 * Worth two different sentences. `model.py` emits one final sample
 * unconditionally after `solve()` returns, so a settled run with NO samples at
 * all did not get as far as solving — an empty shop floor, or a failure during
 * build. A settled run with samples that are all null is the infeasible case,
 * where there is nothing to plot because there was never an incumbent.
 */
function ChartEmpty({ settled }: { settled: boolean }) {
  return (
    <div className="flex h-[190px] items-center justify-center rounded-lg border border-dashed border-edge px-6 text-center">
      <p className="max-w-[40ch] text-[0.74rem] leading-relaxed text-faint">
        {settled
          ? "Nothing plottable from this run. CP-SAT never held a feasible schedule, so there was no incumbent to draw — an infeasible instance, or a search stopped before its first solution."
          : "No solver samples yet. CP-SAT's callback fires only on an improving solution, so a search that has not found its first feasible schedule is silent — that is the solver working, not a stalled stream."}
      </p>
    </div>
  );
}

const tickSeconds = (value: number) => formatDuration(value);

/** Recharts types a tooltip label as `ReactNode`, because a category axis can
 *  carry one. This axis is numeric, so the coercion is safe and the guard only
 *  keeps a stray label from rendering "NaN". */
const tooltipSeconds = (label: unknown) =>
  typeof label === "number" ? formatDuration(label) : String(label ?? "");

/**
 * Whether to draw point markers.
 *
 * A one-point series with `dot={false}` renders as literally nothing, and one
 * point is a routine outcome here rather than an edge case: a run that proves
 * infeasibility, or one whose search ends before a second sample is due,
 * emits only the unconditional final sample.
 */
function singleSample(points: readonly JobshopPoint[]): boolean {
  return points.length === 1;
}

export function CpSatConvergenceChart({ snapshot }: ModelViewProps) {
  const points = deriveJobshopSeries(snapshot.progress);
  // The gap alone is not enough: it is null until BOTH sides exist, so a run
  // with a bound and no incumbent still has a line worth drawing.
  if (!hasPlottable(points, ["incumbent", "bestBound", "gapPercent"])) {
    return <ChartEmpty settled={snapshot.terminal} />;
  }
  const dot = singleSample(points);

  return (
    <div className="h-[190px]">
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={points} margin={{ top: 6, right: 4, bottom: 2, left: 0 }}>
          <CartesianGrid stroke="var(--c-line)" strokeDasharray="2 3" vertical={false} />
          <XAxis
            dataKey="elapsed"
            type="number"
            domain={["dataMin", "dataMax"]}
            tickFormatter={tickSeconds}
            tick={AXIS}
            stroke="var(--c-line)"
          />
          <YAxis
            yAxisId="obj"
            tick={AXIS}
            stroke="var(--c-line)"
            width={54}
            domain={["auto", "auto"]}
          />
          <YAxis
            yAxisId="gap"
            orientation="right"
            tick={AXIS}
            stroke="var(--c-line)"
            width={44}
            unit="%"
            domain={[0, "auto"]}
          />
          <Tooltip contentStyle={TOOLTIP_STYLE} labelFormatter={tooltipSeconds} />
          <Legend wrapperStyle={{ fontSize: "0.68rem", color: "var(--c-dim)" }} />
          {/* `connectNulls` stays off everywhere. The stretch before the first
              feasible schedule is a real fact about the run, and bridging it
              would draw a makespan through a period when none existed. */}
          <Line
            yAxisId="obj"
            type="monotone"
            dataKey="incumbent"
            name="makespan (min)"
            stroke="var(--c-accent)"
            strokeWidth={1.8}
            dot={dot}
            isAnimationActive={false}
            connectNulls={false}
          />
          <Line
            yAxisId="obj"
            type="monotone"
            dataKey="bestBound"
            name="best_bound"
            stroke="var(--c-info)"
            strokeWidth={1.6}
            strokeDasharray="5 3"
            dot={dot}
            isAnimationActive={false}
            connectNulls={false}
          />
          <Line
            yAxisId="gap"
            type="monotone"
            dataKey="gapPercent"
            name="relative_gap %"
            stroke="var(--c-dim)"
            strokeWidth={1.3}
            strokeDasharray="1.5 3"
            dot={dot}
            isAnimationActive={false}
            connectNulls={false}
          />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}

export function ImprovementChart({ snapshot }: ModelViewProps) {
  const points = deriveJobshopSeries(snapshot.progress);
  if (!hasPlottable(points, ["solutionsFound", "conflictsLog", "branches"])) {
    return <ChartEmpty settled={snapshot.terminal} />;
  }
  const dot = singleSample(points);

  return (
    <div className="h-[190px]">
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={points} margin={{ top: 6, right: 4, bottom: 2, left: 0 }}>
          <CartesianGrid stroke="var(--c-line)" strokeDasharray="2 3" vertical={false} />
          <XAxis
            dataKey="elapsed"
            type="number"
            domain={["dataMin", "dataMax"]}
            tickFormatter={tickSeconds}
            tick={AXIS}
            stroke="var(--c-line)"
          />
          {/* Conflicts are CP-SAT's own currency — learned clauses, the thing a
              clause-sharing portfolio actually spends its time producing — and
              they cross orders of magnitude within a second, so a linear axis
              would spend its whole height on the last sample. `conflictsLog`
              is null below 1 because a log axis has no zero. */}
          <YAxis
            yAxisId="conflicts"
            scale="log"
            domain={[1, "auto"]}
            allowDataOverflow
            tick={AXIS}
            stroke="var(--c-line)"
            width={54}
          />
          <YAxis
            yAxisId="solutions"
            orientation="right"
            tick={AXIS}
            stroke="var(--c-line)"
            width={40}
            allowDecimals={false}
            domain={[0, "auto"]}
          />
          <Tooltip contentStyle={TOOLTIP_STYLE} labelFormatter={tooltipSeconds} />
          <Legend wrapperStyle={{ fontSize: "0.68rem", color: "var(--c-dim)" }} />
          {/* The line ends before the last sample on every run: `conflicts` is
              ABSENT on the final sample, which is emitted after the solve
              returns with no callback in hand to read it from. That hole is
              real and is not bridged. */}
          <Line
            yAxisId="conflicts"
            type="monotone"
            dataKey="conflictsLog"
            name="conflicts"
            stroke="var(--c-good)"
            strokeWidth={1.7}
            dot={dot}
            isAnimationActive={false}
            connectNulls={false}
          />
          {/* A step, not a curve. `solutions_found` is a counter, and
              interpolating it would draw fractional improvements between two
              samples that CP-SAT never reported. */}
          <Line
            yAxisId="solutions"
            type="stepAfter"
            dataKey="solutionsFound"
            name="solutions_found"
            stroke="var(--c-accent)"
            strokeWidth={1.5}
            dot={dot}
            isAnimationActive={false}
            connectNulls={false}
          />
          {/* The marks are the events that pace the shop floor: one per sample
              reporting a higher count than any before it. On this solver they
              are rare and each one means a strictly better schedule. */}
          <Scatter
            yAxisId="solutions"
            dataKey="solutionMark"
            name="improvement"
            fill="var(--c-accent)"
            isAnimationActive={false}
          />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}
