/**
 * Current objective against best fare — the chart this model exists to need.
 *
 * `current_objective` gets worse on purpose. Accepting an uphill move is how
 * annealing leaves a local optimum, so a chart of the current walk alone shows
 * a line that repeatedly falls and reads as a model failing. `primary_metric`
 * (`best_fare`) alone is monotonic and reads as a model that never struggles —
 * it also hides everything that makes this annealing rather than hill
 * climbing. The two together are the only honest rendering, which is exactly
 * why the model splits them across `primary_metric` and `payload`.
 *
 * Points where the walk is over the shift are marked, in the plainest colour
 * on the page. Being over capacity is not an error condition: the objective
 * already prices the overrun (that dip in the current line IS the penalty),
 * the incumbent only ever moves on a feasible state, and the crossing is how
 * the search escapes a knapsack that is full. A red marker here would be the
 * single most misleading thing this view could do.
 */

import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { ModelViewProps } from "@/components/models/contract";
import { usePrefersReducedMotion } from "@/components/models/useReducedMotion";

import {
  AXIS_TICK,
  CHART_COLORS,
  CHART_HEIGHT,
  formatFare,
  formatIteration,
  LEGEND_STYLE,
  TOOLTIP_STYLE,
} from "./chartTokens";
import { EmptyPlot } from "./EmptyPlot";
import {
  buildPoints,
  emptyProgressReason,
  traceDomain,
  traceSeries,
} from "./series";

export function SearchTraceChart({ state, snapshot }: ModelViewProps) {
  const reducedMotion = usePrefersReducedMotion();
  const series = traceSeries(buildPoints(snapshot.progress));
  const domain = traceDomain(series);

  if (domain === null) return <EmptyPlot>{emptyProgressReason(state)}</EmptyPlot>;

  return (
    <ResponsiveContainer width="100%" height={CHART_HEIGHT}>
      <LineChart data={series} margin={{ top: 6, right: 8, bottom: 0, left: -12 }}>
        <CartesianGrid stroke={CHART_COLORS.grid} strokeDasharray="3 3" />
        <XAxis
          dataKey="iteration"
          type="number"
          // An explicit domain rather than Recharts' default, so a run with a
          // single reported point still gets a readable axis instead of a
          // collapsed one — see `traceDomain`.
          domain={domain}
          tick={AXIS_TICK}
          tickFormatter={formatIteration}
          stroke={CHART_COLORS.axis}
        />
        <YAxis
          tick={AXIS_TICK}
          tickFormatter={formatFare}
          stroke={CHART_COLORS.axis}
          width={52}
        />
        <Tooltip
          contentStyle={TOOLTIP_STYLE}
          labelFormatter={(label) => `iteration ${formatIteration(Number(label))}`}
        />
        <Legend wrapperStyle={LEGEND_STYLE} />
        <Line
          // Held, not interpolated. The incumbent improves at some unknown
          // moment between two samples, so a straight line between them claims
          // a gradual climb that did not happen; a step says "at least this
          // much, by here", which is all the samples support.
          type="stepAfter"
          dataKey="best"
          name="best fare (kept)"
          stroke={CHART_COLORS.best}
          strokeWidth={2}
          dot={false}
          // A null here is a sanitised non-finite metric, not a reset to zero.
          // Bridging it is closer to the truth than a hole would be.
          connectNulls
          isAnimationActive={!reducedMotion}
        />
        <Line
          type="linear"
          dataKey="current"
          name="current objective"
          stroke={CHART_COLORS.current}
          strokeWidth={1.25}
          dot={false}
          connectNulls={false}
          isAnimationActive={!reducedMotion}
        />
        <Line
          dataKey="currentOverShift"
          name="over the shift (expected)"
          // Zero-width stroke rather than `stroke="none"`: the legend swatch
          // takes its colour from `stroke`, and this series exists only for
          // its markers.
          stroke={CHART_COLORS.overShift}
          strokeWidth={0}
          legendType="circle"
          dot={{
            r: 3,
            fill: CHART_COLORS.overShiftFill,
            stroke: CHART_COLORS.overShift,
            strokeWidth: 1.5,
          }}
          activeDot={false}
          connectNulls={false}
          isAnimationActive={false}
        />
      </LineChart>
    </ResponsiveContainer>
  );
}
