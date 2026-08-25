/**
 * How good the fits that worked actually are.
 *
 * One dot per group, plus the running median — which is `primary_metric`
 * itself, labelled `median_r_squared` by the model. Median rather than mean so
 * one pathological group cannot move the headline of a 180-group run, and
 * **higher is better here**, which is the opposite of `forecasting`'s metric.
 * The direction is read off the payload's `metric_higher_is_better` rather
 * than assumed, because a direction copied between model directories is a bug
 * nothing catches until a chart is drawing "better" downward.
 *
 * Failed groups are ABSENT from this chart, and that is not an omission to fix
 * — a group that could not be fitted has no R-squared, so plotting it at zero
 * would invent a number and plotting it at the axis floor would invent a
 * different one. Their absence is exactly why the second card exists, and the
 * footer says so rather than leaving the gaps to be read as missing data.
 *
 * A fitted group can ALSO have a null R-squared: when the response never moves
 * there is no variance to explain, so the metric is undefined on a perfectly
 * good fit. Those are counted separately rather than being folded in with the
 * failures.
 */

import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  ReferenceLine,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { isSettled, type ModelViewProps } from "@/components/models/contract";
import { EMPTY, formatCount } from "@/lib/format";

import {
  buildGroupPoints,
  formatRSquared,
  metricHigherIsBetter,
  readCounts,
  type GroupPoint,
} from "./panelModel";

const AXIS = { fontSize: 10, fill: "var(--c-dim)", fontFamily: "var(--font-mono)" };
const CHART_HEIGHT = 224;

interface Row {
  done: number;
  groupR2: number | null;
  median: number | null;
  key: string | null;
}

function toRows(points: readonly GroupPoint[]): Row[] {
  return points.map((point) => ({
    done: point.done,
    // Only a FITTED group's R-squared belongs on the quality axis. A failed
    // group's is null anyway, but reading the status makes that a stated rule
    // rather than a coincidence of the payload.
    groupR2: point.status === "failed" ? null : point.rSquared,
    median: point.median,
    key: point.label ?? point.key,
  }));
}

/**
 * The y domain, computed rather than left to `auto`.
 *
 * R-squared is bounded above by 1 by construction and unbounded below — a fit
 * worse than the group's own mean is legitimately negative — so the top is
 * pinned at 1 and only the floor moves. Pinning the top is what makes two runs
 * comparable at a glance; letting it float would make a run whose best fit is
 * 0.3 look like a run of perfect fits.
 */
function domainOf(rows: readonly Row[]): [number, number] {
  let low = 0;
  for (const row of rows) {
    if (row.groupR2 !== null) low = Math.min(low, row.groupR2);
    if (row.median !== null) low = Math.min(low, row.median);
  }
  return [low < 0 ? low - 0.05 : 0, 1];
}

export function FitQualityChart({ state, snapshot }: ModelViewProps) {
  const points = buildGroupPoints(snapshot.progress);
  const rows = toRows(points);
  const counts = readCounts(snapshot.latestProgress);
  const higherIsBetter = metricHigherIsBetter(snapshot.latestProgress);
  const settled = isSettled(state);

  const plotted = rows.filter((row) => row.groupR2 !== null).length;
  const undefinedMetric = points.filter(
    (point) => point.status === "fitted" && point.rSquared === null,
  ).length;
  const median = points.at(-1)?.median ?? null;

  if (plotted === 0) {
    return (
      <div
        className="flex flex-col justify-center gap-1 text-[0.74rem] text-dim"
        style={{ minHeight: CHART_HEIGHT }}
      >
        <p className="font-semibold text-ink">
          {points.length === 0 ? "No groups reported yet." : "No group has a fit to plot."}
        </p>
        <p className="max-w-[46ch] leading-relaxed">
          {points.length === 0
            ? "One progress message is emitted per group by default, so the first dot arrives with the first fitted group."
            : counts.allFailed
              ? "Every group processed so far failed to fit, so there is no R-squared to draw. The reasons are in the card beside this one — that is the chart for this run, not this one."
              : "The groups reported so far were fitted but have no R-squared: their response never moves, so there is no variance to explain."}
        </p>
      </div>
    );
  }

  const [low, high] = domainOf(rows);

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 font-mono text-[0.7rem] text-dim">
        <span>
          median R² <span className="font-semibold text-ink">{formatRSquared(median) ?? EMPTY}</span>
        </span>
        <span>
          groups plotted <span className="font-semibold text-ink">{formatCount(plotted)}</span>
        </span>
        <span className="text-faint">
          {higherIsBetter ? "higher is better" : "lower is better"}
        </span>
      </div>

      <ResponsiveContainer width="100%" height={CHART_HEIGHT}>
        <LineChart data={rows} margin={{ top: 8, right: 12, bottom: 4, left: 4 }}>
          <CartesianGrid stroke="var(--c-line)" strokeDasharray="2 3" />
          <XAxis
            dataKey="done"
            type="number"
            domain={["dataMin", "dataMax"]}
            tick={AXIS}
            stroke="var(--c-edge)"
            tickLine={false}
          />
          <YAxis
            width={44}
            domain={[low, high]}
            tick={AXIS}
            stroke="var(--c-edge)"
            tickLine={false}
            tickFormatter={(value: number) => value.toFixed(1)}
          />
          {/* Zero is where a fit stops beating the group's own mean. Only drawn
              when something actually went below it. */}
          {low < 0 && <ReferenceLine y={0} stroke="var(--c-edge)" strokeDasharray="3 3" />}
          <Tooltip
            cursor={{ stroke: "var(--c-edge)", strokeDasharray: "2 3" }}
            labelFormatter={(value: unknown) => `group ${String(value)}`}
            contentStyle={{
              background: "var(--c-raised)",
              border: "1px solid var(--c-edge)",
              borderRadius: 8,
              fontSize: "0.72rem",
              fontFamily: "var(--font-mono)",
              color: "var(--c-ink)",
            }}
            // Loosely typed on purpose: recharts hands its formatters
            // `ValueType | undefined`, so a `number` parameter does not satisfy
            // the signature even though every value here is one.
            formatter={(value: unknown) =>
              typeof value === "number" ? (formatRSquared(value) ?? EMPTY) : String(value ?? "")
            }
          />
          {/* Dots, not a line: these are independent fits over unrelated
              groups, and a line between them would imply a series where there
              is only an ordering by group key. */}
          <Line
            dataKey="groupR2"
            stroke="none"
            strokeWidth={0}
            dot={{ r: 1.9, fill: "var(--c-info)", stroke: "none" }}
            activeDot={{ r: 3.2, fill: "var(--c-info)", stroke: "none" }}
            connectNulls={false}
            isAnimationActive={false}
          />
          {/* No entry animation. The series extending to the right as groups
              finish is the motion here; replaying a draw-in on every progress
              message would animate the whole history again each time. */}
          <Line
            type="monotone"
            dataKey="median"
            stroke="var(--c-accent)"
            strokeWidth={1.6}
            dot={false}
            connectNulls
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>

      <div className="flex flex-wrap gap-x-4 gap-y-1 text-[0.68rem] text-dim">
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-1.5 w-1.5 rounded-full bg-info" />
          one group
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-0 w-5 border-t-2 border-accent" />
          running median
        </span>
        <span className="text-faint">
          {counts.failed !== null && counts.failed > 0
            ? `${formatCount(counts.failed)} group${counts.failed === 1 ? "" : "s"} ${settled ? "were" : "have been"} left off this chart: a group that could not be fitted has no R-squared. The card beside this one is where they are.`
            : "x is the group's position in the panel, not a time axis — the groups are independent and ordered by key."}
          {undefinedMetric > 0
            ? ` ${formatCount(undefinedMetric)} fitted group${undefinedMetric === 1 ? " is" : "s are"} also absent: a flat response has no variance to explain, so R-squared is undefined on a perfectly good fit.`
            : ""}
        </span>
      </div>
    </div>
  );
}
