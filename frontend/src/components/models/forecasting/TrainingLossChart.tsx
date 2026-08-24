/**
 * Training loss — `payload.train_loss` against `primary_metric`.
 *
 * The correction that matters (gaps note §B1): `val_loss` is not a payload
 * key. The model emits it as `primary_metric` with
 * `primary_metric_label: "val_loss"`, and the payload carries `train_loss`
 * and `best_val_loss`. So this is one chart drawing two series from two
 * different places in the same message, which is easy to get wrong by reading
 * the design doc instead of `models/forecasting/model.py`.
 *
 * `best_val_loss` is drawn as a horizontal reference line rather than a third
 * series: it is the model's own early-stopping tracker, it is monotone, and
 * it is the checkpoint `results()` will be written from — so "the floor the
 * forecast came from" is the useful reading, not "a line that mostly overlaps
 * val_loss".
 */

import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { formatMetric } from "@/lib/format";
import type { ModelViewProps } from "../contract";
import { usePrefersReducedMotion } from "../useReducedMotion";
import { METRIC_LABEL, TREND_CLASS, TREND_LABEL, metricTrend } from "./metric";
import { trainingSummary } from "./series";
import { AXIS_PROPS, CHART, GRID_PROPS, TOOLTIP_PROPS } from "./theme";

const CHART_HEIGHT = 190;

export function TrainingLossChart({ state, snapshot }: ModelViewProps) {
  const reduced = usePrefersReducedMotion();
  const { points, epochsTotal, bestValLoss, latest, previous, dataSynthetic } =
    trainingSummary(snapshot.progress);

  if (points.length === 0) {
    return (
      <EmptyState
        hydrated={snapshot.hydrated}
        state={state}
        droppedProgress={snapshot.droppedProgress}
      />
    );
  }

  const trend = metricTrend(previous?.valLoss, latest?.valLoss);

  return (
    <div>
      <ResponsiveContainer width="100%" height={CHART_HEIGHT}>
        <LineChart data={points} margin={{ top: 6, right: 10, bottom: 2, left: -12 }}>
          <CartesianGrid {...GRID_PROPS} />
          <XAxis
            dataKey="epochLabel"
            {...AXIS_PROPS}
            // The payload's `epoch` is 0-based; a "0 / 40" tick reads as
            // nothing having happened, so the axis shows epoch + 1.
            label={undefined}
            allowDecimals={false}
          />
          <YAxis
            {...AXIS_PROPS}
            width={54}
            tickFormatter={(value: number) => formatShort(value)}
            domain={["auto", "auto"]}
          />
          <Tooltip
            {...TOOLTIP_PROPS}
            formatter={(value: unknown) => formatMetric(typeof value === "number" ? value : null)}
            labelFormatter={(value: unknown) => `epoch ${String(value)}`}
          />
          <Legend
            verticalAlign="top"
            height={22}
            wrapperStyle={{ fontSize: "0.68rem", color: CHART.dim }}
          />
          {bestValLoss !== null && (
            <ReferenceLine
              y={bestValLoss}
              stroke={CHART.good}
              strokeDasharray="4 4"
              strokeWidth={1}
              label={{
                value: `best ${formatShort(bestValLoss)}`,
                position: "insideBottomRight",
                fill: CHART.good,
                fontSize: 9,
              }}
            />
          )}
          <Line
            type="monotone"
            dataKey="trainLoss"
            name="train_loss (payload)"
            stroke={CHART.info}
            strokeWidth={1.6}
            dot={false}
            connectNulls={false}
            isAnimationActive={!reduced}
          />
          <Line
            type="monotone"
            dataKey="valLoss"
            name="val_loss (primary_metric)"
            stroke={CHART.accent}
            strokeWidth={1.8}
            strokeDasharray="5 3"
            dot={false}
            connectNulls={false}
            isAnimationActive={!reduced}
          />
        </LineChart>
      </ResponsiveContainer>

      <dl className="mt-3 flex flex-wrap gap-x-6 gap-y-1 text-[0.7rem]">
        <Stat label={`${METRIC_LABEL} (latest)`} value={formatMetric(latest?.valLoss)} />
        <Stat label="best_val_loss" value={formatMetric(bestValLoss)} />
        <Stat
          label="epoch"
          value={
            latest !== null
              ? `${latest.epochLabel}${epochsTotal !== null ? ` / ${epochsTotal}` : ""}`
              : "—"
          }
        />
        <div className="flex items-baseline gap-1.5">
          <dt className="text-dim">trend</dt>
          {/* Lower is better for this model. `metricTrend` owns that fact;
              this component never compares the two numbers itself. */}
          <dd className={`font-mono font-semibold ${TREND_CLASS[trend]}`}>
            {TREND_LABEL[trend]}
          </dd>
        </div>
      </dl>

      {dataSynthetic === true && (
        <p className="mt-2 text-[0.68rem] text-warn">
          `data_synthetic: true` — this run fell back to the generated series, not
          the samples catalog. The losses are real; the data they were measured on
          is not the workspace's.
        </p>
      )}
      {snapshot.droppedProgress > 0 && (
        <p className="mt-2 text-[0.68rem] text-faint">
          {snapshot.droppedProgress} older progress messages were dropped from the
          client cache, so this chart starts later than the run did.
        </p>
      )}
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
 * `snapshot.progress` can be empty for a long time, or forever — this model
 * trains in under a second, so a client that attaches late can legitimately
 * see a terminal status with no progress at all. That is not an error and it
 * is not a spinner; it is a statement about what was observed.
 */
function EmptyState({
  hydrated,
  state,
  droppedProgress,
}: {
  hydrated: boolean;
  state: ModelViewProps["state"];
  droppedProgress: number;
}) {
  const settled = state === "SUCCEEDED" || state === "FAILED" || state === "CANCELLED";
  return (
    <div
      style={{ minHeight: CHART_HEIGHT }}
      className="flex flex-col justify-center gap-1 rounded-lg border border-dashed border-line px-4 py-6 text-[0.74rem] text-dim"
    >
      {!hydrated ? (
        <p>Reading cached history…</p>
      ) : settled ? (
        <p>
          No progress messages were seen for this run. Forecasting trains in under
          a second, so a client that attached late can miss every epoch. The
          durable telemetry is in Delta; the forecast below still comes from{" "}
          <code className="font-mono">results()</code>.
        </p>
      ) : (
        <p>
          Waiting for the first epoch. This model emits one progress message per
          epoch, and none until training starts.
        </p>
      )}
      {droppedProgress > 0 && (
        <p className="text-faint">{droppedProgress} messages dropped from the cache.</p>
      )}
    </div>
  );
}

/** Losses here are MSE on a standardised scale, so they are small and close
 *  together. Fixed significant digits keep the axis from collapsing to "0". */
function formatShort(value: number): string {
  if (!Number.isFinite(value)) return "—";
  if (value !== 0 && Math.abs(value) < 0.001) return value.toExponential(1);
  return value.toPrecision(3);
}
