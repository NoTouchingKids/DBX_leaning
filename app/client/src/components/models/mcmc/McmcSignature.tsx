/**
 * MCMC's signature: an ensemble of walkers wandering, drawing together as the
 * chains converge.
 *
 * Per `contract.ts`, this is a state machine keyed to the run lifecycle, with
 * exactly one real quantity in it — the radius the ensemble occupies, mapped
 * from `max_rhat` by `spreadForRhat`. The dashed contours are a fixed
 * reference, not a posterior, and no dot corresponds to a chain. The real
 * per-chain coordinates live in the payload (`chain_positions`) and are
 * plotted in the trace chart, which is where a value belongs.
 *
 * The previous version moved the walkers by re-seeding every position from a
 * hash on a 520ms timer. Two things were wrong with that, and both matter more
 * here than anywhere else on the platform. It was not a walk — a walker
 * teleported across the disc between frames, which is noise, not exploration.
 * And 520ms is well inside what `motion.ts` calls a strobe, on the one model
 * whose runs are long enough that this panel is genuinely left open for ten
 * minutes.
 *
 * What replaced it splits the two motions apart:
 *
 *   base position   data. Set by the spread, moves only when a progress
 *                   message moves it, eased by a spring so a stream of
 *                   messages reads as the cloud breathing rather than jumping.
 *   wander          decoration. Declarative keyframes on the compositor, with
 *                   no timer and no React state, so a model emitting progress
 *                   ten times a second cannot restart or stutter it.
 *
 * Six states. `INFEASIBLE` is not reachable for a sampler, but the state type
 * includes it, so it gets the same flat terminal treatment as the rest rather
 * than a crash or a blank.
 */

import { motion } from "motion/react";
import { useMemo } from "react";

import type { ModelViewProps } from "@/components/models/contract";
import { DURATION, EASE, phaseOf, SPRING, staggerFor } from "@/components/models/motion";
import { usePrefersReducedMotion } from "@/components/models/useReducedMotion";
import type { UiRunState } from "@/lib/envelope";
import { EMPTY, formatCount, formatMetric } from "@/lib/format";

import { mcmcPayload, spreadForRhat } from "./payload";
import { phaseFor, walkerPositions } from "./walkers";

/** More dots than this and the canvas is a solid disc, which stops showing the
 *  spread — the one thing it is for. The header says so when a run has more
 *  chains than this draws, rather than quietly reporting the cap as the count. */
const MAX_WALKERS = 16;

/** Dots to draw before the first progress message says how many chains there
 *  really are. Openly a placeholder: the header shows the chain count as
 *  absent until the sampler reports one, so the picture is never mistaken for
 *  a reading. */
const PLACEHOLDER_WALKERS = 8;

/** The idle scale of the walker group. The inhale is the step from here to 1,
 *  and it has to be a group transform: scaling each walker separately only
 *  makes the dots bigger, it does not move the ensemble apart. */
const SEED_SCALE = 0.72;

/** Waypoints in one wander cycle. First and last are both the walker's base
 *  point, so `repeat: Infinity` loops without a seam. */
const DRIFT_STOPS = 5;

/** Wander amplitude, in pixels rather than percent. A walker's local wander
 *  should look the same whether the panel is 320px or 900px wide, and the
 *  canvas height is fixed, so pixels are the stable unit here. Vertical is
 *  smaller because the cloud itself is compressed vertically (`EXTENT_Y` in
 *  `walkers.ts`); an isotropic wander over an anisotropic cloud reads as a
 *  vertical smear. */
const DRIFT_X = 13;
const DRIFT_Y = 8;

const DOT_CLASS: Record<UiRunState, string> = {
  STARTING: "bg-accent border-accent",
  QUEUED: "bg-idle border-idle",
  RUNNING: "bg-info border-info",
  SUCCEEDED: "bg-good border-good",
  FAILED: "bg-bad border-bad",
  CANCELLED: "bg-idle border-idle opacity-60",
  INFEASIBLE: "bg-warn border-warn",
};

/* Each dot carries a much larger, much fainter disc behind it. It is what
 * makes mixing visible: converged walkers sit on top of each other, their
 * halos stack, and the middle of the canvas goes dense. Eight separated dots
 * of the same size say nothing about whether they are the same distribution. */
const HALO_CLASS: Record<UiRunState, string> = {
  STARTING: "bg-accent/12",
  QUEUED: "bg-idle/10",
  RUNNING: "bg-info/14",
  SUCCEEDED: "bg-good/14",
  FAILED: "bg-bad/12",
  CANCELLED: "bg-idle/10",
  INFEASIBLE: "bg-warn/12",
};

/* The colour swap stays CSS rather than `motion`, because it is the one thing
 * in the panel that must survive reduced motion — a run that says "succeeded"
 * by turning green still turns green, just instantly, which is what
 * `motion-reduce:transition-none` does. The timing is bound to the shared
 * vocabulary instead of a Tailwind `duration-*` class so it cannot drift from
 * the settling gesture it arrives with. */
const COLOUR_TRANSITION = {
  transitionDuration: `${DURATION.slow}s`,
  transitionTimingFunction: `cubic-bezier(${EASE.standard.join(",")})`,
} as const;

const CAPTION: Record<UiRunState, [string, string]> = {
  STARTING: ["Starting sampler", "initialising chains"],
  QUEUED: ["Queued", "waiting for compute"],
  RUNNING: ["Sampling posterior", "chains exploring"],
  SUCCEEDED: ["Sampling complete", "chains settled"],
  FAILED: ["Sampling failed", "the run did not complete"],
  CANCELLED: ["Sampling cancelled", "stopped before the last draw"],
  INFEASIBLE: ["Sampling stopped", "reported infeasible"],
};

/**
 * Deterministic [0,1) from two integers.
 *
 * `walkers.ts` has one of these and does not export it. Four duplicated lines
 * cost less than widening that module's API for something purely decorative,
 * and nothing requires the two to agree — this one only shapes a wander.
 */
function hash01(a: number, b: number): number {
  let h = Math.imul(a ^ 0x9e3779b9, 0x85ebca6b) ^ Math.imul(b + 0x165667b1, 0xc2b2ae35);
  h = Math.imul(h ^ (h >>> 13), 0x27d4eb2f);
  h ^= h >>> 16;
  return (h >>> 0) / 0x1_0000_0000;
}

interface Drift {
  /** Pixel offsets from the walker's base point, one per waypoint. */
  x: number[];
  y: number[];
  /** Seconds for one full pass of each axis. The two differ, so the axes fall
   *  out of step and the path never closes; walkers differ from each other,
   *  so the ensemble never lines up into a pattern a viewer can learn. */
  xSeconds: number;
  ySeconds: number;
}

function driftsFor(count: number): Drift[] {
  return Array.from({ length: count }, (_, i) => {
    const stops = (axis: number, amplitude: number) =>
      Array.from({ length: DRIFT_STOPS }, (_, k) =>
        k === 0 || k === DRIFT_STOPS - 1
          ? 0
          : (hash01(i, k * 2 + axis) * 2 - 1) * amplitude,
      );
    // One hop per DURATION.ambient is the floor and never less: motion.ts puts
    // the strobe threshold at about 1.5s, and the spread above the floor is
    // what stops eight walkers breathing in unison.
    const cycle = DURATION.ambient * (DRIFT_STOPS - 1);
    return {
      x: stops(0, DRIFT_X),
      y: stops(1, DRIFT_Y),
      xSeconds: cycle * (1 + hash01(i, 91) * 0.45),
      ySeconds: cycle * (1 + hash01(i, 97) * 0.45),
    };
  });
}

export function McmcSignature({ state, snapshot }: ModelViewProps) {
  const reduced = usePrefersReducedMotion();

  const latest = snapshot.latestProgress;
  const payload = mcmcPayload(latest);
  const rhat = latest?.primary_metric ?? null;
  const spread = spreadForRhat(rhat);

  // `per_chain_acceptance` is the fallback because a payload carrying one
  // entry per chain has answered the question even when `chains` is missing.
  // Zero is not an answer, so it collapses to null with the rest of the
  // unknowns — the header renders that as absent rather than as "0 chains".
  const reported =
    typeof payload.chains === "number" && Number.isFinite(payload.chains)
      ? Math.round(payload.chains)
      : (payload.per_chain_acceptance?.length ?? 0);
  const chains = reported > 0 ? reported : null;
  const drawn = Math.min(MAX_WALKERS, chains ?? PLACEHOLDER_WALKERS);

  // Two phase machines, deliberately: `phaseOf` is the shared timing one every
  // signature switches on, `phaseFor` is this model's placement one and is
  // what `walkers.ts` is tested against. They map one to one.
  const phase = phaseOf(state);
  // Tick 0, always. Advancing it re-seeds every position, which is the
  // teleport this rework removed; the ensemble's motion is the drift below.
  // Passing 0 is a documented mode of that function, not a workaround.
  const points = walkerPositions({
    phase: phaseFor(state),
    count: drawn,
    spread,
    tick: 0,
  });

  // Keyframe arrays have to be referentially stable or `motion` restarts the
  // wander. This model is the platform's streaming stress test — progress
  // arrives many times a second — so this is a correctness memo, not a
  // performance one.
  const drifts = useMemo(() => driftsFor(drawn), [drawn]);

  const stagger = staggerFor(drawn);
  const drifting = phase === "running" && !reduced;
  const key = state ?? "QUEUED";
  const [headline, sub] = CAPTION[key];

  const drawsDone = payload.draws_done;
  const drawsTotal = payload.draws_total;

  // The stagger belongs to the two gestures that have a direction — the
  // ensemble arriving, and the ensemble coming to rest. Applying it to the
  // running spring as well would make every r-hat update ripple across the
  // cloud, which at this model's message rate is a shimmer.
  const positionTransition = (index: number) => {
    if (reduced) return { duration: 0 };
    if (phase === "starting") {
      return { duration: DURATION.inhale, ease: EASE.decelerate, delay: index * stagger };
    }
    if (phase === "settled") {
      return { duration: DURATION.slow, ease: EASE.emphasis, delay: index * stagger };
    }
    return SPRING.soft;
  };

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
            {chains === null ? EMPTY : formatCount(chains)}
            {chains !== null && drawn < chains && (
              <span
                className="text-faint"
                title={`The run has ${chains} chains. Beyond ${MAX_WALKERS} dots the canvas is a solid disc and stops showing the spread, so only ${drawn} are drawn.`}
              >
                {` (${drawn} drawn)`}
              </span>
            )}
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

          {/* The inhale, and the only thing in the frame that expands. It ends
              on a held still picture rather than a loop: a cold job can take
              forty seconds to say anything, and a pulse over that would be
              describing waiting as if it were work. */}
          <motion.div
            className="absolute inset-0"
            initial={false}
            animate={{ scale: phase === "idle" ? SEED_SCALE : 1 }}
            transition={
              reduced
                ? { duration: 0 }
                : {
                    duration: phase === "starting" ? DURATION.inhale : DURATION.slow,
                    ease: EASE.decelerate,
                  }
            }
          >
            {points.map((point, index) => {
              const drift = drifts[index];
              return (
                <motion.div
                  key={index}
                  className="absolute"
                  // No `style` for left/top and no mount animation: `motion`
                  // then holds these as percentages and interpolates them as
                  // percentages. Seeding them from `style` instead makes the
                  // first read a computed pixel value, which cannot tween to
                  // a percentage and snaps.
                  initial={false}
                  animate={{
                    left: `${point.x}%`,
                    top: `${point.y}%`,
                    opacity: point.visible ? 1 : 0,
                  }}
                  transition={positionTransition(index)}
                >
                  <motion.div
                    className="relative h-0 w-0"
                    animate={
                      drifting && drift !== undefined
                        ? { x: drift.x, y: drift.y }
                        : { x: 0, y: 0 }
                    }
                    transition={
                      drifting && drift !== undefined
                        ? {
                            x: {
                              duration: drift.xSeconds,
                              repeat: Infinity,
                              ease: EASE.standard,
                            },
                            y: {
                              duration: drift.ySeconds,
                              repeat: Infinity,
                              ease: EASE.standard,
                            },
                          }
                        : // Coming to rest is part of the terminal gesture, not
                          // a separate one: the walker eases back to its base
                          // point on the same curve that carries it to the ring.
                          { duration: reduced ? 0 : DURATION.slow, ease: EASE.emphasis }
                    }
                  >
                    <span
                      style={COLOUR_TRANSITION}
                      className={
                        `absolute -mt-[12px] -ml-[12px] h-[24px] w-[24px] rounded-full ` +
                        `transition-colors motion-reduce:transition-none ${HALO_CLASS[key]}`
                      }
                    />
                    {/* The dot's own opacity class (CANCELLED dims to 60%)
                        multiplies with the wrapper's animated opacity rather
                        than fighting it, which the previous single-element
                        version had to work around inline. */}
                    <span
                      style={COLOUR_TRANSITION}
                      className={
                        `absolute -mt-[4.5px] -ml-[4.5px] h-[9px] w-[9px] rounded-full border ` +
                        `transition-colors motion-reduce:transition-none ${DOT_CLASS[key]}`
                      }
                    />
                  </motion.div>
                </motion.div>
              );
            })}
          </motion.div>
        </div>
      </div>
    </section>
  );
}
