/**
 * The cooling schedule, and what it is buying.
 *
 * This chart earns the second slot because it is the receipt for the signature
 * animation: the lattice claims to be paced by `temperature`, and this is that
 * number, unretouched. It is also the one diagnostic that says whether the
 * schedule was right. The model cools geometrically, so on a log axis a
 * healthy run is a straight line — a kink means the bounds were overridden
 * into something else. Acceptance rate on the right axis is the consequence:
 * it should fall with the temperature and be near zero by the end. Acceptance
 * still high at the last iteration means the search never settled and the run
 * was, in effect, still random when it stopped.
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
  formatIteration,
  formatPercentTick,
  formatTemperatureTick,
  LEGEND_STYLE,
  TOOLTIP_STYLE,
} from "./chartTokens";
import { EmptyPlot } from "./EmptyPlot";
import { buildPoints, coolingSeries, emptyProgressReason } from "./series";
import type { CoolingPoint } from "./series";

/**
 * A log axis needs a range. One sample, or a schedule whose temperature never
 * moved, gives `min === max`, and Recharts cannot place ticks on a log scale
 * across a zero-width domain — it renders nothing at all. Falling back to
 * linear costs the straight-line reading and keeps the point on screen.
 */
function temperatureScale(series: readonly CoolingPoint[]): "log" | "linear" {
  let min = Infinity;
  let max = -Infinity;
  for (const point of series) {
    if (point.temperature < min) min = point.temperature;
    if (point.temperature > max) max = point.temperature;
  }
  return min > 0 && max > min ? "log" : "linear";
}

export function CoolingChart({ state, snapshot }: ModelViewProps) {
  const reducedMotion = usePrefersReducedMotion();
  const series = coolingSeries(buildPoints(snapshot.progress));

  if (series.length === 0) {
    return <EmptyPlot>{emptyProgressReason(state)}</EmptyPlot>;
  }

  return (
    <ResponsiveContainer width="100%" height={CHART_HEIGHT}>
      <LineChart data={series} margin={{ top: 6, right: 4, bottom: 0, left: -12 }}>
        <CartesianGrid stroke={CHART_COLORS.grid} strokeDasharray="3 3" />
        <XAxis
          dataKey="iteration"
          type="number"
          domain={["dataMin", "dataMax"]}
          tick={AXIS_TICK}
          tickFormatter={formatIteration}
          stroke={CHART_COLORS.axis}
        />
        <YAxis
          yAxisId="temperature"
          scale={temperatureScale(series)}
          domain={["auto", "auto"]}
          tick={AXIS_TICK}
          tickFormatter={formatTemperatureTick}
          stroke={CHART_COLORS.axis}
          width={52}
        />
        <YAxis
          yAxisId="acceptance"
          orientation="right"
          // Fixed 0..1: acceptance is a rate, and letting the axis rescale to
          // the observed range would make a run that fell from 4% to 1% look
          // like the same collapse as one that fell from 90% to 2%.
          domain={[0, 1]}
          tick={AXIS_TICK}
          tickFormatter={formatPercentTick}
          stroke={CHART_COLORS.axis}
          width={40}
        />
        <Tooltip
          contentStyle={TOOLTIP_STYLE}
          labelFormatter={(label) => `iteration ${formatIteration(Number(label))}`}
        />
        <Legend wrapperStyle={LEGEND_STYLE} />
        <Line
          yAxisId="temperature"
          type="monotone"
          dataKey="temperature"
          name="temperature"
          stroke={CHART_COLORS.temperature}
          strokeWidth={2}
          dot={false}
          isAnimationActive={!reducedMotion}
        />
        <Line
          yAxisId="acceptance"
          type="monotone"
          dataKey="acceptanceRate"
          name="acceptance rate"
          stroke={CHART_COLORS.acceptance}
          strokeWidth={1.25}
          strokeDasharray="4 3"
          dot={false}
          connectNulls
          isAnimationActive={!reducedMotion}
        />
      </LineChart>
    </ResponsiveContainer>
  );
}
