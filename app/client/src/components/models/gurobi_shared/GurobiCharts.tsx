/**
 * The two diagnostics charts, shared by both Gurobi models.
 *
 * These are the honest half of the page: every value drawn here came off the
 * wire in a `progress` message. The signature animation next to them is
 * allowed to be decorative precisely because these are not.
 *
 * Shared rather than duplicated for the same reason `mipSeries.ts` is: one
 * sampler, one payload. What differs between the models is only what the
 * objective MEANS — staffing cost against roster preference for scheduling,
 * distance-weighted route cost for routing — so that arrives as a label.
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
import { deriveMipSeries, hasPlottable } from "./mipSeries";

const AXIS = { fill: "var(--c-faint)", fontSize: 10 } as const;
const TOOLTIP_STYLE = {
  background: "var(--c-raised)",
  border: "1px solid var(--c-edge)",
  borderRadius: 8,
  fontSize: "0.72rem",
  color: "var(--c-ink)",
} as const;

/**
 * The state every chart here spends its first seconds in, and some runs spend
 * their whole life in.
 *
 * `job/drivers/gurobi.py` only samples on `MIP` callbacks, throttled to one
 * every two seconds. Presolve, the root relaxation and a model that proves
 * infeasibility without branching all produce zero progress messages — so
 * "nothing yet" is an outcome, not a spinner, and it says which.
 */
function ChartEmpty({ settled }: { settled: boolean }) {
  return (
    <div className="flex h-[190px] items-center justify-center rounded-lg border border-dashed border-edge px-6 text-center">
      <p className="max-w-[38ch] text-[0.74rem] leading-relaxed text-faint">
        {settled
          ? "This run reported no branch-and-bound samples. The solver finished before it started branching — presolve, the root relaxation or an immediate infeasibility."
          : "No branch-and-bound samples yet. The driver reports roughly every two seconds once the solver starts branching; presolve and the root relaxation are silent."}
      </p>
    </div>
  );
}

const tickSeconds = (value: number) => formatDuration(value);

/** Recharts types a tooltip label as `ReactNode`, because a category axis can
 *  carry one. This axis is numeric, so the coercion is safe and the guard is
 *  only here to keep a stray label from rendering "NaN". */
const tooltipSeconds = (label: unknown) =>
  typeof label === "number" ? formatDuration(label) : String(label ?? "");

export function MipConvergenceChart({
  snapshot,
  objectiveLabel = "objective",
}: ModelViewProps & { objectiveLabel?: string }) {
  const points = deriveMipSeries(snapshot.progress);
  const settled = snapshot.terminal;
  // The gap alone is not enough to draw this chart: it is null until BOTH
  // sides exist, so a run that has a bound and no incumbent still has a line.
  if (!hasPlottable(points, ["incumbent", "bestBound", "gapPercent"])) {
    return <ChartEmpty settled={settled} />;
  }

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
          {/* `connectNulls` stays off everywhere: the pre-feasible stretch of
              a MIP is a real fact about the run, and bridging it would draw a
              line through solutions that did not exist. */}
          <Line
            yAxisId="obj"
            type="monotone"
            dataKey="incumbent"
            name={`incumbent ${objectiveLabel}`}
            stroke="var(--c-accent)"
            strokeWidth={1.8}
            dot={false}
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
            dot={false}
            isAnimationActive={false}
            connectNulls={false}
          />
          <Line
            yAxisId="gap"
            type="monotone"
            dataKey="gapPercent"
            name="mip_gap %"
            stroke="var(--c-dim)"
            strokeWidth={1.3}
            strokeDasharray="1.5 3"
            dot={false}
            isAnimationActive={false}
            connectNulls={false}
          />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}

export function SearchActivityChart({ snapshot }: ModelViewProps) {
  const points = deriveMipSeries(snapshot.progress);
  if (!hasPlottable(points, ["nodesLog", "solutionCount"])) {
    return <ChartEmpty settled={snapshot.terminal} />;
  }

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
          {/* Node counts cross orders of magnitude within a minute, so a
              linear axis spends its whole height on the last sample. `nodesLog`
              is null below 1 because a log axis has no zero — the line starts
              where branching starts. */}
          <YAxis
            yAxisId="nodes"
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
          <Line
            yAxisId="nodes"
            type="monotone"
            dataKey="nodesLog"
            name="nodes_explored"
            stroke="var(--c-good)"
            strokeWidth={1.7}
            dot={false}
            isAnimationActive={false}
            connectNulls={false}
          />
          {/* A step, not a curve: `solution_count` is a counter sampled every
              two seconds, and interpolating it would draw fractional
              incumbents between samples. */}
          <Line
            yAxisId="solutions"
            type="stepAfter"
            dataKey="solutionCount"
            name="solution_count"
            stroke="var(--c-accent)"
            strokeWidth={1.5}
            dot={false}
            isAnimationActive={false}
            connectNulls={false}
          />
          {/* The marks are the same events that pace the signature animation:
              one per sample that reported a higher count than any before it. */}
          <Scatter
            yAxisId="solutions"
            dataKey="incumbentMark"
            name="new incumbent"
            fill="var(--c-accent)"
            isAnimationActive={false}
          />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}
