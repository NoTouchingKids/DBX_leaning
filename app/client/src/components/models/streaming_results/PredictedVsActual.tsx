/**
 * Predicted vs actual, growing as chunks arrive.
 *
 * The only chart this model gets, on purpose: it carries both stories at
 * once. Each `result` message is one completed backtest window — its own
 * `chunk_index`, `final: false` until the last — so the series extending to
 * the right IS the progress indicator, and the two lines diverging or
 * tracking IS the result. A second card would be the same information twice.
 *
 * Rows are appended, never replaced. `row_count` is that chunk's count, not a
 * running total, and a chunk reporting zero rows is stated rather than
 * hidden: it is the difference between "wrote nothing" and "the write
 * failed", and those are indistinguishable if a zero renders as blankness.
 */

import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { isSettled, type ModelViewProps } from "@/components/models/contract";
import { formatCount, formatMetric } from "@/lib/format";

import { accumulateChunks, arrivalState } from "./streamingModel";

const AXIS = { fontSize: 10, fill: "var(--c-dim)", fontFamily: "var(--font-mono)" };

const ARRIVAL_TEXT: Record<string, string> = {
  none: "no chunks yet",
  arriving: "still arriving",
  complete: "complete — final chunk seen",
  stopped: "incomplete — the run ended before a final chunk",
};

const ARRIVAL_TONE: Record<string, string> = {
  none: "border-line text-faint",
  arriving: "border-info text-info",
  complete: "border-good text-good",
  stopped: "border-warn text-warn",
};

export function PredictedVsActual({ state, snapshot }: ModelViewProps) {
  const view = accumulateChunks(snapshot.results);
  const arrival = arrivalState(view, isSettled(state));

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 font-mono text-[0.7rem] text-dim">
        <span>
          chunks <span className="font-semibold text-ink">{formatCount(view.chunks.length)}</span>
        </span>
        <span>
          rows written{" "}
          <span className={view.totalRows === 0 ? "font-semibold text-warn" : "font-semibold text-ink"}>
            {formatCount(view.totalRows)}
          </span>
        </span>
        <span
          className={`rounded-full border px-2 py-0.5 text-[0.66rem] ${ARRIVAL_TONE[arrival] ?? ""}`}
        >
          {ARRIVAL_TEXT[arrival]}
        </span>
      </div>

      {view.emptyChunks.length > 0 && (
        <p className="text-[0.7rem] text-warn">
          {view.emptyChunks.length === 1
            ? `Chunk ${view.emptyChunks[0]} wrote 0 rows durably.`
            : `Chunks ${view.emptyChunks.join(", ")} wrote 0 rows durably.`}{" "}
          Reported, not inferred — a zero here is how a window that produced nothing is told apart
          from one whose write never happened.
        </p>
      )}
      {view.missing.length > 0 && (
        <p className="text-[0.7rem] text-warn">
          Chunk {view.missing.join(", ")} never arrived. The chart is drawn from the chunks that
          did, so there is a hole in it rather than a shortened series.
        </p>
      )}

      {view.points.length === 0 ? (
        <div className="flex h-[13rem] flex-col justify-center gap-1 text-[0.74rem] text-dim">
          <p className="font-semibold text-ink">
            {view.chunks.length === 0
              ? "No windows have finished yet."
              : "Chunks arrived with no chartable rows."}
          </p>
          <p>
            {view.chunks.length === 0
              ? isSettled(state)
                ? "This run produced no result chunks at all — not an empty chart, an empty run."
                : "One chunk of predicted-vs-actual rows is emitted per completed backtest window, mid-run. The first arrives once the first window has been fit."
              : "The rows carried no origin/step pair to place them on the series."}
          </p>
        </div>
      ) : (
        <ResponsiveContainer width="100%" height={224}>
          <LineChart data={[...view.points]} margin={{ top: 8, right: 12, bottom: 4, left: 4 }}>
            <CartesianGrid stroke="var(--c-line)" strokeDasharray="2 3" />
            <XAxis
              dataKey="x"
              type="number"
              domain={["dataMin", "dataMax"]}
              tick={AXIS}
              stroke="var(--c-edge)"
              tickLine={false}
            />
            <YAxis
              width={56}
              tick={AXIS}
              stroke="var(--c-edge)"
              tickLine={false}
              tickFormatter={(value: number) => formatMetric(value)}
            />
            <Tooltip
              cursor={{ stroke: "var(--c-edge)", strokeDasharray: "2 3" }}
              labelFormatter={(value: unknown) => `series index ${String(value)}`}
              contentStyle={{
                background: "var(--c-raised)",
                border: "1px solid var(--c-edge)",
                borderRadius: 8,
                fontSize: "0.72rem",
                fontFamily: "var(--font-mono)",
                color: "var(--c-ink)",
              }}
              // Loosely typed on purpose: recharts hands its formatters
              // `ValueType | undefined`, so a `number` parameter does not
              // satisfy the signature even though every value here is one.
              formatter={(value: unknown) =>
                typeof value === "number" ? formatMetric(value) : String(value ?? "")
              }
            />
            {/* No entry animation. The series growing to the right as chunks
                land is the motion here; replaying a draw-in on every chunk
                would animate the whole history again each time, which reads
                as the data changing when it has only been added to. */}
            <Line
              type="monotone"
              dataKey="actual"
              stroke="var(--c-ink)"
              strokeWidth={1.6}
              dot={false}
              connectNulls={false}
              isAnimationActive={false}
            />
            <Line
              type="monotone"
              dataKey="predicted"
              stroke="var(--c-accent)"
              strokeWidth={1.6}
              strokeDasharray="4 3"
              dot={false}
              connectNulls={false}
              isAnimationActive={false}
            />
          </LineChart>
        </ResponsiveContainer>
      )}

      <div className="flex flex-wrap gap-x-4 gap-y-1 text-[0.68rem] text-dim">
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-0 w-5 border-t-2 border-ink" />
          actual
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-0 w-5 border-t-2 border-dashed border-accent" />
          predicted
        </span>
        <span className="text-faint">
          x is the absolute series index (origin + step), so successive windows sit side by side
          rather than on top of one another.
        </span>
      </div>
    </div>
  );
}
