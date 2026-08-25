/**
 * Live trace — every walker's position in one parameter, against draws.
 *
 * This chart was blocked when the design was written: the payload carried no
 * per-chain coordinates. It does now — `models/mcmc/model.py::_progress` emits
 * `chain_positions` (and `chain_positions_truncated`), one point per chain per
 * progress sample, capped at `MAX_TRACE_CHAINS`. So the chart is built here,
 * against the field that exists, not the field the design imagined.
 *
 * What a reader is looking for is *mixing*: traces that overlap and wander
 * through the same band are the same distribution, and one line sitting flat
 * or off on its own is the chain that `stuck_chains` is counting. That is why
 * every chain is drawn in one colour rather than eight — chain #5's identity
 * is not the question — and why a stuck chain is the exception, drawn in
 * alarm so the two diagnostics agree with each other on screen.
 *
 * The model sends a snapshot per message, never a history; the history is
 * assembled client-side from the progress this tab holds. On a long run that
 * is thousands of messages, so it is downsampled to a display budget before
 * Recharts ever sees it — see `buildTrace`.
 */

import { useState } from "react";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, XAxis, YAxis } from "recharts";

import type { ModelViewProps } from "@/components/models/contract";
import { formatCount } from "@/lib/format";

import { buildTrace, MAX_TRACE_POINTS } from "./payload";

export function TraceChart({ snapshot }: ModelViewProps) {
  const [parameterIndex, setParameterIndex] = useState(0);
  const trace = buildTrace(snapshot.progress, parameterIndex);

  if (trace.rows.length === 0) {
    return (
      <p className="px-1 py-10 text-center text-[0.75rem] text-faint">
        No chain positions yet. Each progress message carries one point per
        chain, so the trace grows from whatever this tab has received — it is
        empty before the first message, and short if you joined a run late.
      </p>
    );
  }

  // A parameter the payload does not name is still plottable by index, but
  // unlabelled — say index rather than invent a name.
  const label = (index: number) => trace.parameters[index] ?? `param ${index}`;
  const parameterCount = Math.max(trace.parameters.length, 1);

  return (
    <div>
      {parameterCount > 1 && (
        <div className="mb-3 flex flex-wrap gap-1.5">
          {Array.from({ length: parameterCount }, (_, index) => (
            <button
              key={index}
              type="button"
              onClick={() => setParameterIndex(index)}
              aria-pressed={index === parameterIndex}
              className={
                `rounded-md border px-2 py-0.5 font-mono text-[0.68rem] ` +
                (index === parameterIndex
                  ? "border-accent bg-accent-soft text-accent-ink"
                  : "border-line bg-paper text-dim hover:border-edge")
              }
            >
              {label(index)}
            </button>
          ))}
        </div>
      )}

      <div className="h-[200px] w-full text-faint">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={trace.rows} margin={{ top: 4, right: 8, bottom: 0, left: -12 }}>
            {/* `currentColor` throughout, not a `stroke-*` class: Recharts
                sets its own stroke presentation attribute on each shape, and
                that beats an inherited class. See ChainHealthChart. */}
            <CartesianGrid strokeDasharray="3 3" stroke="currentColor" className="text-line" />
            <XAxis
              dataKey="x"
              type="number"
              domain={["dataMin", "dataMax"]}
              tickLine={false}
              axisLine={false}
              tick={{ fill: "currentColor", fontSize: 10 }}
            />
            <YAxis
              tickLine={false}
              axisLine={false}
              width={52}
              tick={{ fill: "currentColor", fontSize: 10 }}
              tickFormatter={(value: number) =>
                Math.abs(value) >= 1000 || (value !== 0 && Math.abs(value) < 0.01)
                  ? value.toExponential(1)
                  : String(Math.round(value * 1000) / 1000)
              }
            />
            {trace.chainKeys.map((key, index) => (
              <Line
                key={key}
                dataKey={key}
                type="linear"
                dot={false}
                isAnimationActive={false}
                stroke="currentColor"
                strokeWidth={trace.stuckChains.has(index) ? 1.6 : 1}
                strokeOpacity={trace.stuckChains.has(index) ? 1 : 0.5}
                className={trace.stuckChains.has(index) ? "text-bad" : "text-info"}
                // A chain can be missing from a message the others are in;
                // joining across the hole is a smaller lie than a gap that
                // looks like the chain died.
                connectNulls
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>

      <p className="mt-2 text-[0.66rem] leading-relaxed text-faint">
        {formatCount(trace.chainKeys.length)} chains ·{" "}
        <code className="font-mono">{label(parameterIndex)}</code> against draws
        {trace.sourcePoints > MAX_TRACE_POINTS &&
          ` · ${formatCount(trace.sourcePoints)} progress points thinned to ${formatCount(trace.rows.length)} for display`}
        {trace.hiddenChains > 0 &&
          ` · ${formatCount(trace.hiddenChains)} further chains not drawn`}
        {trace.truncatedUpstream &&
          " · the model capped this ensemble before sending it, so some chains are absent from the payload entirely"}
      </p>
    </div>
  );
}
