/**
 * The two Beta posteriors, drawn from their own parameters.
 *
 * This is the natural chart for a conjugate model and it is nearly free: the
 * payload carries `posterior_alpha` and `posterior_beta` per arm, so the whole
 * density is two numbers and some arithmetic — no round trip, no sampling, no
 * server-side rendering of a picture.
 *
 * It is also the chart that has to survive this model's defining awkwardness.
 * The parameters come from the latest progress payload if one arrived and from
 * the result rows if none did, which is the same view either way; and they are
 * null before the `posteriors` stage, which is a state this chart says out
 * loud rather than drawing as a flat line at zero.
 */

import { Area, AreaChart, CartesianGrid, ResponsiveContainer, XAxis, YAxis } from "recharts";

import type { ModelViewProps } from "@/components/models/contract";

import { densityRows, densityWindow } from "./beta";
import { armsFromSnapshot, decisionFromSnapshot, type ArmView } from "./derive";

/** An arm whose posteriors stage has run. */
type FittedArm = ArmView & { posteriorAlpha: number; posteriorBeta: number };

/**
 * `text-*` plus `fill`/`stroke="currentColor"`, not `fill-*`/`stroke-*`.
 * Recharts writes its own fill and stroke presentation attributes onto every
 * shape, and a presentation attribute beats an inherited CSS value — so a
 * colour class on the wrapper loses silently and the chart comes out in
 * Recharts' default blue, in both palettes. `currentColor` is what carries a
 * design token into the SVG.
 */
const SERIES_CLASS = ["text-info", "text-accent"];
const SWATCH_CLASS = ["bg-info", "bg-accent"];

export function PosteriorChart({ snapshot }: ModelViewProps) {
  const { arms, source } = armsFromSnapshot(snapshot);
  const decision = decisionFromSnapshot(snapshot);

  const fitted = arms.filter(
    (arm): arm is FittedArm => arm.posteriorAlpha !== null && arm.posteriorBeta !== null,
  );

  if (fitted.length === 0) {
    return (
      <p className="px-1 py-10 text-center text-[0.75rem] text-faint">
        {arms.length === 0
          ? "No arms reported yet."
          : "Arms are counted, but the posteriors stage has not run — posterior_alpha and posterior_beta are null until it does."}
      </p>
    );
  }

  const window = densityWindow(
    fitted.map((arm) => ({ alpha: arm.posteriorAlpha, beta: arm.posteriorBeta })),
  );
  const rows = densityRows(
    fitted.map((arm, index) => ({
      key: `s${index}`,
      alpha: arm.posteriorAlpha,
      beta: arm.posteriorBeta,
    })),
    window,
  );

  return (
    <div>
      <div className="mb-2 flex flex-wrap gap-x-4 gap-y-1 text-[0.68rem]">
        {fitted.map((arm, index) => (
          <span key={arm.role} className="flex items-center gap-1.5">
            <span
              className={`inline-block h-2 w-2 rounded-full ${SWATCH_CLASS[index] ?? "bg-info"}`}
            />
            <span className="text-dim">
              {arm.label}
              <span className="ml-1 font-mono text-faint">
                Beta({arm.posteriorAlpha.toFixed(1)}, {arm.posteriorBeta.toFixed(1)})
              </span>
            </span>
          </span>
        ))}
      </div>

      <div className="h-[190px] w-full text-faint">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={rows} margin={{ top: 4, right: 8, bottom: 0, left: -34 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="currentColor" className="text-line" />
            <XAxis
              dataKey="x"
              type="number"
              domain={window}
              tickLine={false}
              axisLine={false}
              tick={{ fill: "currentColor", fontSize: 10 }}
              tickFormatter={(value: number) => value.toFixed(3)}
            />
            {/* The y-axis is density, whose units are per-unit-rate and mean
                nothing to a reader. Kept for the gridline, stripped of ticks
                rather than shown as numbers nobody should compare. */}
            <YAxis tick={false} tickLine={false} axisLine={false} width={40} />
            {fitted.map((_, index) => (
              <Area
                key={index}
                dataKey={`s${index}`}
                type="monotone"
                isAnimationActive={false}
                stroke="currentColor"
                fill="currentColor"
                strokeWidth={1.5}
                fillOpacity={0.18}
                className={SERIES_CLASS[index] ?? "text-info"}
              />
            ))}
          </AreaChart>
        </ResponsiveContainer>
      </div>

      <p className="mt-2 text-[0.66rem] leading-relaxed text-faint">
        Success rate on the x-axis, windowed to where the two posteriors
        actually live — on thousands of trials they are needles, and the full
        0–1 interval would hide whether they overlap.
        {decision.outcome !== null && ` Success = ${decision.outcome}.`}
        {source === "results" &&
          " Read from the result rows: this run finished before any progress message reached this tab."}
      </p>
    </div>
  );
}
