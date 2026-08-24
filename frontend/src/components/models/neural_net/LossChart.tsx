/**
 * Cross-entropy loss across both progress levels.
 *
 * The x axis is the model's own batch-step counter
 * (`epoch * batches_per_epoch + batch + 1`), not `epoch` — see the header of
 * `series.ts` for why keying on `epoch` puts several points on one x and what
 * the alternatives cost. Epoch boundaries are drawn as vertical reference
 * lines so the coarser level is still legible inside the finer one.
 *
 * Unlike `forecasting`, `val_loss` IS a payload key here, so both series come
 * from `payload` and neither is `primary_metric` (which on this model is
 * `val_accuracy`, and is on the other chart, where it belongs next to its
 * baseline).
 *
 * `train_loss` is a running mean over the epoch so far, which is why it steps
 * down inside an epoch and can jump at the boundary; `val_loss` is a fresh
 * `_evaluate()` on every message, including the batch-level ones. They are
 * therefore not the same kind of number, and the caption says so.
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

import { EMPTY, formatMetric } from "@/lib/format";
import type { ModelViewProps } from "../contract";
import { usePrefersReducedMotion } from "../useReducedMotion";
import { trainingSummary } from "./series";
import { AXIS_PROPS, CHART, GRID_PROPS, TOOLTIP_PROPS } from "./theme";

const CHART_HEIGHT = 190;
/** Past this, the boundary lines are denser than the data between them. */
const MAX_BOUNDARY_LINES = 24;

export function LossChart({ snapshot }: ModelViewProps) {
  const reduced = usePrefersReducedMotion();
  const { points, epochPoints, batchCount, latest, epochsTotal, device, dataSynthetic } =
    trainingSummary(snapshot.progress);

  if (points.length === 0) {
    return (
      <div
        style={{ minHeight: CHART_HEIGHT }}
        className="flex flex-col justify-center rounded-lg border border-dashed border-line px-4 py-6 text-[0.74rem] text-dim"
      >
        <p>
          No progress messages yet. When they arrive there will be two kinds on one
          stream: <code className="font-mono">level: &quot;batch&quot;</code> a couple
          of times per epoch and{" "}
          <code className="font-mono">level: &quot;epoch&quot;</code> once at each
          epoch end.
        </p>
      </div>
    );
  }

  const boundaries =
    epochPoints.length <= MAX_BOUNDARY_LINES ? epochPoints : [];

  return (
    <div>
      <ResponsiveContainer width="100%" height={CHART_HEIGHT}>
        <LineChart data={points} margin={{ top: 6, right: 10, bottom: 2, left: -12 }}>
          <CartesianGrid {...GRID_PROPS} />
          <XAxis dataKey="step" {...AXIS_PROPS} allowDecimals={false} />
          <YAxis
            {...AXIS_PROPS}
            width={54}
            domain={["auto", "auto"]}
            tickFormatter={(value: number) => (Number.isFinite(value) ? value.toPrecision(3) : EMPTY)}
          />
          <Tooltip
            {...TOOLTIP_PROPS}
            labelFormatter={(value: unknown) => `batch step ${String(value)}`}
            formatter={(value: unknown, name: unknown) => [
              formatMetric(typeof value === "number" ? value : null),
              String(name),
            ]}
          />
          <Legend
            verticalAlign="top"
            height={22}
            wrapperStyle={{ fontSize: "0.68rem", color: CHART.dim }}
          />
          {boundaries.map((point) => (
            <ReferenceLine
              key={point.seq}
              x={point.step}
              stroke={CHART.edge}
              strokeDasharray="2 3"
              strokeWidth={1}
            />
          ))}
          <Line
            type="monotone"
            dataKey="trainLoss"
            name="train_loss (running mean)"
            stroke={CHART.info}
            strokeWidth={1.6}
            dot={false}
            connectNulls={false}
            isAnimationActive={!reduced}
          />
          <Line
            type="monotone"
            dataKey="valLoss"
            name="val_loss (re-evaluated each message)"
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
        <Stat label="train_loss" value={formatMetric(latest?.trainLoss)} />
        <Stat label="val_loss" value={formatMetric(latest?.valLoss)} />
        <Stat label="grad_norm" value={formatMetric(latest?.gradNorm)} />
        <Stat label="learning_rate" value={formatMetric(latest?.learningRate)} />
        <Stat
          label="samples"
          value={`${epochPoints.length} epoch · ${batchCount} batch${
            epochsTotal !== null ? ` · ${epochsTotal} epochs planned` : ""
          }`}
        />
      </dl>

      {/* `device` is the one field that keeps a CPU run and a GPU run apart
          after the fact, and nothing else on the page carries it. */}
      <p className="mt-2 text-[0.68rem] text-faint">
        torch device <span className="font-mono text-dim">{device ?? EMPTY}</span>
        {dataSynthetic === null
          ? ""
          : dataSynthetic
            ? " · generated fallback data, not the samples catalog"
            : " · real samples-catalog trips"}
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
