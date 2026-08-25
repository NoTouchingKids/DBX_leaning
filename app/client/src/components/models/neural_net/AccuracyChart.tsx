/**
 * Validation accuracy against the majority-class baseline.
 *
 * The baseline is not decoration. The classes are ~55/30/15 on purpose
 * (`cut_quantiles` defaults to 0.55/0.85), so predicting "typical" for
 * everything scores about 0.55, and an accuracy shown on its own flatters
 * this model by roughly that much. `models/neural_net/model.py` carries
 * `baseline_accuracy` in every progress payload for exactly this reason and
 * says "render them on the same axis" — so they are on the same axis.
 *
 * Three series and a reference line:
 *
 *  - `val_accuracy` (`primary_metric`) — every point, both levels.
 *  - `macro_f1` — same 0..1 axis, and the number that notices when the model
 *    quietly stops predicting the 15% class at all.
 *  - `best_val_accuracy` — NULL through the first epoch's batch samples,
 *    because checkpointing only updates at epoch end. Drawn with
 *    `connectNulls={false}` so that gap stays a gap: it means "no epoch has
 *    finished", and a line dropping to zero would say something false.
 *  - the baseline, as a horizontal reference line.
 *
 * The y domain is fixed at 0..1 rather than auto-fitted. Accuracy is bounded,
 * and an auto domain would magnify a two-point wobble into a dramatic climb.
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

import { EMPTY } from "@/lib/format";
import type { ModelViewProps } from "../contract";
import { usePrefersReducedMotion } from "../useReducedMotion";
import {
  METRIC_LABEL,
  TREND_CLASS,
  TREND_LABEL,
  VERDICT_CLASS,
  VERDICT_LABEL,
  baselineVerdict,
  metricTrend,
} from "./metric";
import { trainingSummary } from "./series";
import { AXIS_PROPS, CHART, GRID_PROPS, TOOLTIP_PROPS } from "./theme";

const CHART_HEIGHT = 190;

function pct(value: number | null | undefined): string {
  return value === null || value === undefined || !Number.isFinite(value)
    ? EMPTY
    : `${(value * 100).toFixed(1)}%`;
}

export function AccuracyChart({ state, snapshot }: ModelViewProps) {
  const reduced = usePrefersReducedMotion();
  const summary = trainingSummary(snapshot.progress);
  const { points, epochPoints, latest, previous, baselineAccuracy, bestValAccuracy } = summary;

  if (points.length === 0) {
    return <EmptyState hydrated={snapshot.hydrated} state={state} />;
  }

  // A second, dot-only series carrying accuracy at epoch boundaries only.
  // Cheaper and better typed than a custom `dot` renderer, and it is what
  // keeps the coarser of the two progress levels visible inside the finer.
  const data = points.map((point) => ({
    ...point,
    epochAccuracy: point.level === "epoch" ? point.valAccuracy : null,
  }));

  const trend = metricTrend(previous?.valAccuracy, latest?.valAccuracy);
  const verdict = baselineVerdict(latest?.valAccuracy, baselineAccuracy);
  const lift =
    latest?.valAccuracy !== null && latest?.valAccuracy !== undefined && baselineAccuracy !== null
      ? latest.valAccuracy - baselineAccuracy
      : null;

  return (
    <div>
      <ResponsiveContainer width="100%" height={CHART_HEIGHT}>
        <LineChart data={data} margin={{ top: 6, right: 10, bottom: 2, left: -14 }}>
          <CartesianGrid {...GRID_PROPS} />
          <XAxis dataKey="step" {...AXIS_PROPS} allowDecimals={false} />
          <YAxis
            {...AXIS_PROPS}
            width={44}
            domain={[0, 1]}
            ticks={[0, 0.25, 0.5, 0.75, 1]}
            tickFormatter={(value: number) => `${Math.round(value * 100)}%`}
          />
          <Tooltip
            {...TOOLTIP_PROPS}
            labelFormatter={(value: unknown) => `batch step ${String(value)}`}
            formatter={(value: unknown, name: unknown) => [
              pct(typeof value === "number" ? value : null),
              String(name),
            ]}
          />
          <Legend
            verticalAlign="top"
            height={22}
            wrapperStyle={{ fontSize: "0.68rem", color: CHART.dim }}
          />
          {baselineAccuracy !== null && (
            <ReferenceLine
              y={baselineAccuracy}
              stroke={CHART.warn}
              strokeDasharray="4 4"
              strokeWidth={1}
              label={{
                value: `majority-class baseline ${pct(baselineAccuracy)}`,
                position: "insideTopLeft",
                fill: CHART.warn,
                fontSize: 9,
              }}
            />
          )}
          <Line
            type="monotone"
            dataKey="valAccuracy"
            name="val_accuracy (primary_metric)"
            stroke={CHART.accent}
            strokeWidth={1.8}
            dot={false}
            connectNulls={false}
            isAnimationActive={!reduced}
          />
          <Line
            type="monotone"
            dataKey="macroF1"
            name="macro_f1"
            stroke={CHART.info}
            strokeWidth={1.3}
            dot={false}
            connectNulls={false}
            isAnimationActive={!reduced}
          />
          <Line
            type="stepAfter"
            dataKey="bestValAccuracy"
            name="best_val_accuracy (epoch end only)"
            stroke={CHART.good}
            strokeWidth={1.4}
            strokeDasharray="5 3"
            dot={false}
            // The null run before the first epoch ends is information.
            connectNulls={false}
            isAnimationActive={!reduced}
          />
          <Line
            dataKey="epochAccuracy"
            name="epoch boundary"
            // Dots only: zero-width stroke draws no line but still gives the
            // legend a swatch to colour, which `stroke="none"` would not.
            stroke={CHART.accent}
            strokeWidth={0}
            legendType="circle"
            dot={{ r: 2.6, fill: CHART.accent, stroke: "none" }}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>

      <dl className="mt-3 flex flex-wrap gap-x-6 gap-y-1 text-[0.7rem]">
        <Stat label={METRIC_LABEL} value={pct(latest?.valAccuracy)} />
        <Stat
          label="best_val_accuracy"
          value={bestValAccuracy === null ? `${EMPTY} no epoch finished` : pct(bestValAccuracy)}
        />
        <Stat label="baseline_accuracy" value={pct(baselineAccuracy)} />
        <div className="flex items-baseline gap-1.5">
          <dt className="text-dim">lift</dt>
          <dd className={`font-mono font-semibold ${VERDICT_CLASS[verdict]}`}>
            {lift === null ? EMPTY : `${lift >= 0 ? "+" : ""}${(lift * 100).toFixed(1)}pp`}
          </dd>
        </div>
        <div className="flex items-baseline gap-1.5">
          <dt className="text-dim">trend</dt>
          {/* Higher is better for this model. `metricTrend` in this directory
              owns that; nothing here compares the two numbers itself. */}
          <dd className={`font-mono font-semibold ${TREND_CLASS[trend]}`}>{TREND_LABEL[trend]}</dd>
        </div>
      </dl>

      <p className={`mt-2 text-[0.68rem] ${VERDICT_CLASS[verdict]}`}>{VERDICT_LABEL[verdict]}.</p>

      {bestValAccuracy === null && epochPoints.length === 0 && (
        <p className="mt-1 text-[0.68rem] text-faint">
          <code className="font-mono">best_val_accuracy</code> is null until the first
          epoch ends — checkpointing only updates at an epoch boundary. Null here
          means &ldquo;no epoch has finished&rdquo;, not zero.
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
 * `snapshot.progress` can be empty for a long time, or forever. This model is
 * cheap by design — a few thousand rows and about a second of CPU training —
 * so a client that attaches late can see the terminal status with no progress
 * at all. That is a statement about what was observed, not a loading state.
 */
function EmptyState({
  hydrated,
  state,
}: {
  hydrated: boolean;
  state: ModelViewProps["state"];
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
          No progress messages were seen for this run. This model trains in about a
          second, so attaching late can miss every epoch. The durable per-class
          metrics are in the results table.
        </p>
      ) : (
        <p>
          Waiting for the first sample. Progress arrives at two levels — a couple of
          batch samples per epoch, then one at each epoch end.
        </p>
      )}
    </div>
  );
}
