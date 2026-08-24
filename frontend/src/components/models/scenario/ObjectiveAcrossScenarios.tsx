/**
 * Objective across scenarios — the completion chart.
 *
 * Draws in ONCE, from the `result` preview, rather than accumulating during
 * the run. That is not a stylistic choice: this model emits no per-scenario
 * objective on the progress path (only the running best), so there is no
 * incremental series to draw. The points here are real rows the server chose
 * with LTTB on `preview_axes = ("scenario_index", "objective")` — whole rows,
 * fewer of them, never interpolated.
 *
 * A cancelled sweep still gets a chart: `results()` returns whatever was
 * evaluated, so a partial arc is the correct picture of a partial run.
 */

import {
  CartesianGrid,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { ModelViewProps } from "@/components/models/contract";
import { isSettled } from "@/components/models/contract";
import { usePrefersReducedMotion } from "@/components/models/useReducedMotion";
import { formatCount, formatMetric } from "@/lib/format";

import { objectivePoints } from "./scenarioModel";

const AXIS = { fontSize: 10, fill: "var(--c-dim)", fontFamily: "var(--font-mono)" };

export function ObjectiveAcrossScenarios({ state, snapshot }: ModelViewProps) {
  const reduced = usePrefersReducedMotion();
  const points = objectivePoints(snapshot.results);
  const rowsWritten = snapshot.results.reduce((sum, r) => sum + r.row_count, 0);

  if (points.length === 0) {
    return (
      <div className="flex h-[13rem] flex-col justify-center gap-1 text-[0.74rem] text-dim">
        {snapshot.results.length > 0 ? (
          <>
            <p className="font-semibold text-ink">
              {formatCount(rowsWritten)} rows written, no preview points.
            </p>
            <p>
              The result message arrived and reported its durable row count; it carried no
              chartable <span className="font-mono">scenario_index</span> /{" "}
              <span className="font-mono">objective</span> pair.
            </p>
          </>
        ) : (
          <>
            <p className="font-semibold text-ink">Draws in on completion.</p>
            <p>
              {isSettled(state)
                ? "This run ended without a result message, so there is nothing to draw."
                : "The sweep reports only its running best while it works; every scenario's objective arrives in one result message at the end."}
            </p>
          </>
        )}
      </div>
    );
  }

  return (
    <div>
      <ResponsiveContainer width="100%" height={208}>
        <ScatterChart margin={{ top: 8, right: 12, bottom: 4, left: 4 }}>
          <CartesianGrid stroke="var(--c-line)" strokeDasharray="2 3" />
          <XAxis
            type="number"
            dataKey="scenario_index"
            name="scenario"
            tick={AXIS}
            stroke="var(--c-edge)"
            tickLine={false}
          />
          <YAxis
            type="number"
            dataKey="objective"
            name="objective"
            width={56}
            tick={AXIS}
            stroke="var(--c-edge)"
            tickLine={false}
            tickFormatter={(value: number) => formatMetric(value)}
          />
          <Tooltip
            cursor={{ stroke: "var(--c-edge)", strokeDasharray: "2 3" }}
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
          <Scatter
            data={points}
            fill="var(--c-accent)"
            // The draw-in IS the point of this chart; reduced motion gets the
            // finished plot immediately rather than a different chart.
            isAnimationActive={!reduced}
            animationDuration={700}
          />
        </ScatterChart>
      </ResponsiveContainer>
      <p className="mt-1 text-[0.68rem] text-faint">
        {formatCount(points.length)} preview points of {formatCount(rowsWritten)} rows written.
        Objective against sweep order — the sweep enumerates capacity, then demand, then unit
        cost, so the repeating shape is the inner unit-cost loop, not noise.
      </p>
    </div>
  );
}
