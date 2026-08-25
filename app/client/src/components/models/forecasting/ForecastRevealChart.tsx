/**
 * Forecast reveal — the recursive horizon forecast from `results()`.
 *
 * A different data source and a different moment from the training-loss chart
 * next to it: that one is live `progress`, this one arrives once, at the end,
 * in a `result` message. The digest moved both OUT of a collapsed disclosure
 * into the always-visible diagnostics row, on the grounds that the click was
 * buying nothing — so this renders inline and states its own emptiness rather
 * than hiding behind a summary.
 *
 * The shaded ribbon is +/- `val_mae` — held-out mean absolute error, the same
 * number on every row, so a CONSTANT-width band. It is captioned as one. This
 * model emits no prediction interval, and a band that fanned out with the
 * horizon would be inventing one.
 */

import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { formatCount, formatMetric } from "@/lib/format";
import { isSettled, type ModelViewProps } from "../contract";
import { usePrefersReducedMotion } from "../useReducedMotion";
import { forecastReveal } from "./series";
import { AXIS_PROPS, CHART, GRID_PROPS, TOOLTIP_PROPS } from "./theme";

const CHART_HEIGHT = 190;

export function ForecastRevealChart({ state, snapshot }: ModelViewProps) {
  const reduced = usePrefersReducedMotion();
  const reveal = forecastReveal(snapshot.results);

  if (reveal.points.length === 0) {
    return <EmptyState state={state} rowCount={reveal.rowCount} />;
  }

  return (
    <div>
      <ResponsiveContainer width="100%" height={CHART_HEIGHT}>
        <ComposedChart data={reveal.points} margin={{ top: 6, right: 10, bottom: 2, left: -12 }}>
          <CartesianGrid {...GRID_PROPS} />
          <XAxis dataKey="step" {...AXIS_PROPS} allowDecimals={false} />
          <YAxis
            {...AXIS_PROPS}
            width={54}
            domain={["auto", "auto"]}
            tickFormatter={(value: number) => (Number.isFinite(value) ? value.toPrecision(3) : "—")}
          />
          <Tooltip
            {...TOOLTIP_PROPS}
            labelFormatter={(value: unknown) => `step +${String(value)}`}
            formatter={(value: unknown, name: unknown) =>
              Array.isArray(value)
                ? [`${formatMetric(value[0] as number)} … ${formatMetric(value[1] as number)}`, String(name)]
                : [formatMetric(typeof value === "number" ? value : null), String(name)]
            }
          />
          {reveal.valMae !== null && (
            <Area
              dataKey="band"
              name={`± val_mae (${formatMetric(reveal.valMae)})`}
              stroke="none"
              fill={CHART.accent}
              fillOpacity={0.12}
              // Draws in once. Reduced motion gets the finished frame
              // immediately — the curve is the information, the draw is not.
              isAnimationActive={!reduced}
              animationDuration={900}
            />
          )}
          <Line
            type="monotone"
            dataKey="forecast"
            name="forecast"
            stroke={CHART.accent}
            strokeWidth={1.8}
            dot={false}
            isAnimationActive={!reduced}
            animationDuration={900}
          />
        </ComposedChart>
      </ResponsiveContainer>

      <dl className="mt-3 flex flex-wrap gap-x-6 gap-y-1 text-[0.7rem]">
        <Stat label="val_mae" value={formatMetric(reveal.valMae)} />
        <Stat label="val_rmse" value={formatMetric(reveal.valRmse)} />
        <Stat
          label="epochs_trained"
          value={reveal.epochsTrained === null ? "—" : formatCount(reveal.epochsTrained)}
        />
        <Stat
          label="row_count"
          value={reveal.rowCount === null ? "—" : formatCount(reveal.rowCount)}
        />
      </dl>

      {!reveal.complete && (
        <p className="mt-2 text-[0.68rem] text-warn">
          No <code className="font-mono">final: true</code> result has arrived yet — this
          is a partial view.
        </p>
      )}
      {reveal.dataSynthetic === true && (
        <p className="mt-2 text-[0.68rem] text-warn">
          Forecast of a generated series: the run fell back rather than reading the
          samples catalog.
        </p>
      )}
      <p className="mt-2 text-[0.68rem] text-faint">
        The preview is LTTB-downsampled server-side; the full horizon is in the
        model&apos;s own results table.
      </p>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline gap-1.5">
      <dt className="text-dim">{label}</dt>
      <dd className="font-mono font-semibold">{value}</dd>
    </div>
  );
}

/**
 * Three genuinely different emptinesses, and collapsing them would hide the
 * interesting one.
 *
 * `row_count: 0` on a settled run is not "nothing yet" — `results()` returns
 * an empty list when the run never completed an epoch (cancelled early, or
 * val_loss went non-finite on the first pass and training stopped before any
 * checkpoint was kept). That is a real outcome and it gets said out loud.
 */
function EmptyState({
  state,
  rowCount,
}: {
  state: ModelViewProps["state"];
  rowCount: number | null;
}) {
  const settled = isSettled(state);
  return (
    <div
      style={{ minHeight: CHART_HEIGHT }}
      className="flex flex-col justify-center gap-1 rounded-lg border border-dashed border-line px-4 py-6 text-[0.74rem] text-dim"
    >
      {rowCount === 0 ? (
        <p>
          The run wrote <span className="font-mono font-semibold text-warn">0</span>{" "}
          result rows. Forecasting returns no rows when it never kept a checkpoint —
          cancelled before the first epoch finished, or the loss diverged on the
          first pass. Not a missing chart; a run with no forecast in it.
        </p>
      ) : settled ? (
        <p>
          This run reached a terminal state without a result message on the stream.
          The durable rows may still exist in Delta — backfill, or open the results
          table.
        </p>
      ) : (
        <p>The forecast is written once, at the end of the run. Nothing to draw yet.</p>
      )}
    </div>
  );
}
