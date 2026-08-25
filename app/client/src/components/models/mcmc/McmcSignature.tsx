/**
 * MCMC's signature: an ensemble of walkers drawing in as the chains converge.
 *
 * Per `contract.ts`, this is a state machine keyed to the run lifecycle, with
 * exactly one real quantity in it — the radius the walkers scatter within,
 * mapped from `max_rhat` by `spreadForRhat`. Individual positions are a
 * seeded random walk and mean nothing; the dashed contours are a fixed
 * reference, not a posterior. The real per-chain coordinates now exist in the
 * payload (`chain_positions`) and are plotted in the trace chart, which is
 * where a value belongs.
 *
 * Six states. `INFEASIBLE` is not reachable for a sampler, but the state type
 * includes it, so it gets the same flat terminal treatment as the rest rather
 * than a crash or a blank.
 */

import { useEffect, useState } from "react";

import type { ModelViewProps } from "@/components/models/contract";
import { usePrefersReducedMotion } from "@/components/models/useReducedMotion";
import type { UiRunState } from "@/lib/envelope";
import { EMPTY, formatCount, formatMetric } from "@/lib/format";

import { mcmcPayload, spreadForRhat } from "./payload";
import { phaseFor, walkerPositions } from "./walkers";

/** Milliseconds per random-walk step. Roughly the CSS transition duration, so
 *  each hop finishes about as the next begins and the ensemble looks like it
 *  is moving continuously rather than blinking. */
const STEP_MS = 520;

/** The default is 8 chains. More than this and the canvas is a solid disc,
 *  which stops showing the spread — the one thing it is for. */
const MAX_WALKERS = 16;
const DEFAULT_WALKERS = 8;

const DOT_CLASS: Record<UiRunState, string> = {
  STARTING: "bg-accent border-accent",
  QUEUED: "bg-idle border-idle",
  RUNNING: "bg-info border-info",
  SUCCEEDED: "bg-good border-good",
  FAILED: "bg-bad border-bad",
  CANCELLED: "bg-idle border-idle opacity-60",
  INFEASIBLE: "bg-warn border-warn",
};

const CAPTION: Record<UiRunState, [string, string]> = {
  STARTING: ["Starting sampler", "initialising chains"],
  QUEUED: ["Queued", "waiting for compute"],
  RUNNING: ["Sampling posterior", "chains exploring"],
  SUCCEEDED: ["Sampling complete", "chains settled"],
  FAILED: ["Sampling failed", "the run did not complete"],
  CANCELLED: ["Sampling cancelled", "stopped before the last draw"],
  INFEASIBLE: ["Sampling stopped", "reported infeasible"],
};

/** A counter that advances only while asked to. Null interval = frozen, which
 *  is how reduced motion and every terminal state both stop the walk without
 *  a second code path. */
function useTick(intervalMs: number | null): number {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (intervalMs === null) return;
    const id = setInterval(() => setTick((t) => t + 1), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return tick;
}

export function McmcSignature({ state, snapshot }: ModelViewProps) {
  const reduced = usePrefersReducedMotion();
  // Only RUNNING walks. QUEUED has no chains yet and STARTING has chains that
  // have not stepped, so neither needs a timer — and a terminal run must not
  // keep one alive for a page that could sit open all afternoon.
  const tick = useTick(state === "RUNNING" && !reduced ? STEP_MS : null);

  const latest = snapshot.latestProgress;
  const payload = mcmcPayload(latest);
  const rhat = latest?.primary_metric ?? null;
  const spread = spreadForRhat(rhat);

  const declaredChains =
    typeof payload.chains === "number" && Number.isFinite(payload.chains)
      ? payload.chains
      : (payload.per_chain_acceptance?.length ?? DEFAULT_WALKERS);
  const count = Math.min(MAX_WALKERS, Math.max(1, Math.round(declaredChains)));

  const phase = phaseFor(state);
  const points = walkerPositions({ phase, count, spread, tick });
  const dotClass = DOT_CLASS[state ?? "QUEUED"];
  const [headline, sub] = CAPTION[state ?? "QUEUED"];

  const drawsDone = payload.draws_done;
  const drawsTotal = payload.draws_total;

  return (
    <section className="overflow-hidden rounded-[10px] border border-edge bg-raised">
      <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1 px-4 pt-3.5">
        <div>
          <div className="text-[0.86rem] font-bold">{headline}</div>
          <div className="text-[0.7rem] text-dim">{sub}</div>
        </div>
        <div className="flex flex-wrap gap-x-5 gap-y-1 font-mono text-[0.68rem] text-dim">
          <span>
            <span className="text-faint">max_rhat </span>
            {formatMetric(rhat)}
          </span>
          <span>
            <span className="text-faint">draws </span>
            {typeof drawsDone === "number" ? formatCount(drawsDone) : EMPTY}
            {typeof drawsTotal === "number" ? ` / ${formatCount(drawsTotal)}` : ""}
          </span>
          <span>
            <span className="text-faint">chains </span>
            {formatCount(count)}
          </span>
        </div>
      </div>

      <div className="px-4 pt-3 pb-4">
        <div
          className="relative h-[150px] overflow-hidden rounded-lg border border-line bg-paper"
          // Decorative: the animation's meaning is stated in prose next to it
          // by the page, and a dozen unlabelled dots are noise to a screen
          // reader. The numbers above are the accessible version of this.
          aria-hidden="true"
        >
          <svg
            className="absolute inset-0 h-full w-full"
            viewBox="0 0 100 100"
            preserveAspectRatio="none"
          >
            {[10, 20, 30].map((rx) => (
              <ellipse
                key={rx}
                cx={50}
                cy={50}
                rx={rx}
                ry={rx * 0.6}
                fill="none"
                strokeDasharray="3 3"
                strokeWidth={0.5}
                className="stroke-edge"
              />
            ))}
          </svg>
          {points.map((point, index) => (
            <span
              key={index}
              className={
                `absolute -mt-[4.5px] -ml-[4.5px] h-[9px] w-[9px] rounded-full border ` +
                `transition-[left,top,background-color,opacity] duration-500 ease-out ` +
                `motion-reduce:transition-none ${dotClass}`
              }
              style={{
                left: `${point.x}%`,
                top: `${point.y}%`,
                // Only set when hiding: an inline opacity would otherwise beat
                // the `opacity-60` that CANCELLED carries in its dot class.
                opacity: point.visible ? undefined : 0,
              }}
            />
          ))}
        </div>
      </div>
    </section>
  );
}
