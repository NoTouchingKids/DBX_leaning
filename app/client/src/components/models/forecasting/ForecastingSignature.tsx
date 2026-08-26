/**
 * Forecasting's signature: a horizon timeline that draws in point by point.
 *
 * ## Why this and not the neural net the design digest sketches
 *
 * The digest's own table flags the Three.js neural net as an **open question,
 * not a decision** — because `job/models/forecasting/model.py` is
 * `SGDRegressor.partial_fit` over lag features and says so in its docstring,
 * and the wireframe carries a callout saying "not a neural net". A generic
 * "a model is training" visual describes this model's class less precisely
 * than the other signatures describe theirs, and the digest floats exactly
 * this alternative: "a horizon timeline drawing in point by point with a
 * narrowing band". Three further reasons for taking it:
 *
 *  - Three.js is not a dependency of this app and is a large one to add for a
 *    decorative layer the digest itself says the page must work without.
 *  - A timeline reads as *this* model — history in, horizon out, one point
 *    per step — which is the recognition job a signature exists to do.
 *  - Plain SVG has no lazy-load failure mode to degrade gracefully from.
 *
 * Revisitable: the neural net was a deliberate earlier choice, and nothing
 * here forecloses it. Only this file would change.
 *
 * ## The lifecycle phases, per `motion.ts`
 *
 *   idle      history, the now-divider and 24 unlit horizon dots. No cone —
 *             a queued run has not earned a forecast it has not started — and
 *             nothing moves. QUEUED is idle here because `phaseOf` says so:
 *             a run waiting for compute has nothing to report, and it used to
 *             get the same sweep as a run that was actually working.
 *   starting  the cone unrolls from the now-line, once, over DURATION.inhale,
 *             and then HOLDS open. It does not loop and does not fade back
 *             out — a cold job sits in this frame for tens of seconds, and a
 *             pulse here would be describing waiting as if it were work.
 *   running   markers land left to right as epochs arrive, and one soft sweep
 *             crosses the forecast region per DURATION.ambient.
 *   settled   the markers land, the tone washes to the terminal colour, and
 *             nothing moves again.
 *
 * Two of these are corrections rather than polish.
 *
 * The sweep previously ran ONLY while `percent_complete` was null, so the
 * panel went completely still the moment the first epoch landed: the one
 * phase that has to stay watchable for minutes was the phase with nothing in
 * it, and the transition into it was a loop stopping mid-pass. It now runs
 * for the whole of `running`, at DURATION.ambient rather than the invented
 * 2.6s of the three `<animate>` elements it replaces.
 *
 * The markers stagger because of how fast this model actually is. Forty
 * epochs of `partial_fit` finish in well under a second, so the ordinary case
 * is not markers arriving one at a time — it is `lit` going from 0 to 24
 * between two frames. Blind per-index delays would spread that over 1.4s and
 * describe a run that had already finished; `staggerFor` caps the wave at
 * half a second, which is long enough to read as left-to-right and short
 * enough to still be about the present.
 *
 * ## What is real (mirrored in the view's `honesty` note)
 *
 * Real: how many horizon markers are lit, which tracks `percent_complete`
 * (`100 * (epoch + 1) / epochs`, populated on every message for this model),
 * and the single flat terminal colour.
 *
 * Decorative: the history waveform, the projected path the markers sit on,
 * the width of the band, and the sweep. The band is a drawing device. This
 * model emits no prediction interval of any kind, and a ribbon that narrows
 * is the single easiest thing on the page to misread as one — hence the
 * caption, and hence the fact that it narrows with *elapsed progress* rather
 * than with any measure of uncertainty. The sweep is pacing: it is not tied
 * to an epoch, a step, or anything the job reports.
 */

import { motion } from "motion/react";

import type { UiRunState } from "@/lib/envelope";
import { isSettled, payloadOf, type ModelViewProps } from "../contract";
import type { ForecastingProgressPayload } from "@/lib/models";
import { DURATION, EASE, SPRING, phaseOf, staggerFor } from "../motion";
import { usePrefersReducedMotion } from "../useReducedMotion";

/* Geometry. One viewBox, scaled by CSS — the component fills the width it is
   given and sets its own height through the aspect ratio, per the contract. */
const X0 = 8;
const NOW_X = 148;
const X1 = 392;
const MID_Y = 68;
const HISTORY_STEPS = 40;
const HORIZON_MARKS = 24;

/** How far the sweep travels: the forecast region, and only that. */
const HORIZON_SPAN = X1 - NOW_X;

/**
 * The decorative series. Daily plus weekly seasonality, which is the shape
 * the model's docstring describes its data as having — deterministic, so it
 * does not shimmer between renders and does not need a ref to stay still.
 */
function wave(u: number): number {
  return MID_Y - 19 * Math.sin(u * Math.PI * 5.5) - 5 * Math.sin(u * Math.PI * 13.1);
}

const NOW_U = (NOW_X - X0) / (X1 - X0);

function xAt(u: number): number {
  return X0 + u * (X1 - X0);
}

const HISTORY_PATH = Array.from({ length: HISTORY_STEPS }, (_, i) => {
  const u = (i / (HISTORY_STEPS - 1)) * NOW_U;
  return `${xAt(u).toFixed(1)},${wave(u).toFixed(1)}`;
}).join(" ");

/** Marker `i` sits at this fraction of the whole width. */
function horizonU(i: number): number {
  return NOW_U + ((i + 1) / HORIZON_MARKS) * (1 - NOW_U);
}

/**
 * The cone around the projected path, at `spread` of its full width.
 *
 * Recomputed rather than animated: `spread` only moves when a progress
 * message lands, and one epoch of forty is a two-percent change in width. The
 * one moment it has to move as a gesture — the starting inhale — is a `scaleX`
 * on the whole path instead, which needs no path interpolation at all.
 */
function bandPathFor(spread: number): string {
  const upper: string[] = [];
  const lower: string[] = [];
  for (let i = 0; i <= HORIZON_MARKS; i += 1) {
    const u = i === 0 ? NOW_U : horizonU(i - 1);
    const t = (u - NOW_U) / (1 - NOW_U);
    const half = 24 * t * spread;
    const x = xAt(u).toFixed(1);
    upper.push(`${x},${(wave(u) - half).toFixed(1)}`);
    lower.unshift(`${x},${(wave(u) + half).toFixed(1)}`);
  }
  return `M ${upper.join(" L ")} L ${lower.join(" L ")} Z`;
}

/** One flat colour for the whole visual, taken from the lifecycle state. */
const TONE: Record<UiRunState, string> = {
  STARTING: "text-accent",
  QUEUED: "text-info",
  RUNNING: "text-info",
  SUCCEEDED: "text-good",
  FAILED: "text-bad",
  CANCELLED: "text-idle",
  INFEASIBLE: "text-warn",
};

const MARKER_STAGGER = staggerFor(HORIZON_MARKS);

export function ForecastingSignature({ state, snapshot }: ModelViewProps) {
  const reduced = usePrefersReducedMotion();
  const settled = isSettled(state);
  const phase = phaseOf(state);

  const latest = snapshot.latestProgress;
  const percent = latest?.percent_complete ?? null;
  // `percent_complete: null` is a real value on any message — for this model
  // it should not happen, but "should not" is not a guarantee, and a null
  // must not silently become 0%.
  const fraction =
    percent !== null && Number.isFinite(percent) ? Math.min(1, Math.max(0, percent / 100)) : null;

  // SUCCEEDED means the horizon was reached whatever the last message said;
  // every other terminal state freezes at wherever the run actually got to.
  const litFraction = state === "SUCCEEDED" ? 1 : (fraction ?? 0);
  const lit = Math.round(litFraction * HORIZON_MARKS);

  // The band closes as the run advances. Decorative — see the header.
  const convergence = state === "SUCCEEDED" ? 1 : litFraction;
  const bandPath = bandPathFor(1 - 0.82 * convergence);

  const payload = payloadOf<ForecastingProgressPayload>(latest);
  const epochs =
    typeof payload.epochs_total === "number" ? payload.epochs_total : null;
  const epoch = typeof payload.epoch === "number" ? payload.epoch + 1 : null;

  const label =
    state === null
      ? "No run selected. Horizon timeline idle."
      : `${state.toLowerCase()} — ${lit} of ${HORIZON_MARKS} horizon markers drawn` +
        (epoch !== null && epochs !== null ? `, epoch ${epoch} of ${epochs}` : "");

  return (
    // No card chrome here on purpose: the contract says the page owns layout,
    // and a border drawn by both would double up.
    <div className="w-full">
      <svg
        viewBox={`0 0 400 132`}
        className="h-auto w-full"
        role="img"
        aria-label={label}
      >
        <title>{label}</title>
        {/* The terminal state is one flat wash over the whole visual rather
            than a per-element recolour, so the tone lives here and everything
            below is `currentColor`. */}
        <g
          className={
            `transition-colors duration-300 motion-reduce:transition-none ` +
            (state === null ? "text-faint" : TONE[state])
          }
        >
          {/* Baseline and the "now" divider. Deliberately unanimated: they are
              the reference everything else is read against, and a reference
              that moves is not one. */}
          <line
            x1={X0}
            y1={112}
            x2={X1}
            y2={112}
            stroke="currentColor"
            strokeWidth={1}
            opacity={0.2}
          />
          <line
            x1={NOW_X}
            y1={12}
            x2={NOW_X}
            y2={112}
            stroke="currentColor"
            strokeWidth={1}
            strokeDasharray="3 4"
            opacity={0.45}
          />

          {/* Observed history — decorative. */}
          <polyline
            points={HISTORY_PATH}
            fill="none"
            stroke="currentColor"
            strokeWidth={1.6}
            strokeLinejoin="round"
            opacity={0.4}
          />

          {/* The cone, and the inhale.
              Absent in idle, so it MOUNTS on the move to STARTING and its
              `initial` is what plays there — one unroll from the now-line,
              decelerating into a held frame.

              `originX: 0` and not `style.transformOrigin`: for SVG, motion
              builds transform-origin from the origin* values it finds in the
              animation target and then overwrites whatever the style prop
              said, so an origin set in `style` is silently replaced by the
              50% default and the cone opens from its own middle. Against
              motion's `transform-box: fill-box`, 0 is the left edge of the
              path's box, which is exactly NOW_X.

              Under reduced motion the cone is simply open: a run has been
              asked for, and that is the information — the unroll was only
              ever the transition. */}
          {phase !== "idle" && (
            <motion.path
              d={bandPath}
              fill="currentColor"
              opacity={0.09}
              initial={
                phase === "starting" && !reduced ? { scaleX: 0, originX: 0 } : false
              }
              animate={{ scaleX: 1, originX: 0 }}
              transition={{ duration: DURATION.inhale, ease: EASE.decelerate }}
            />
          )}

          {/* Horizon markers, drawn in left to right. The marker count is the
              one real thing here, so it is the one thing that survives reduced
              motion — instantly, rather than not at all. */}
          {Array.from({ length: HORIZON_MARKS }, (_, i) => {
            const u = horizonU(i);
            const on = i < lit;
            return (
              <motion.circle
                key={i}
                cx={xAt(u)}
                cy={wave(u)}
                fill="currentColor"
                // `initial={false}`, never a mount entrance: this component is
                // mounted for the life of a run, and a run opened from history
                // arrives already finished. Its markers are a record of epochs
                // that happened before the page existed, and washing them in
                // would animate the past.
                initial={false}
                animate={{
                  r: on ? 3 : 1.8,
                  opacity: on ? (settled ? 0.9 : 1) : 0.18,
                }}
                transition={
                  reduced
                    ? { duration: 0 }
                    : { ...SPRING.snappy, delay: on ? i * MARKER_STAGGER : 0 }
                }
              />
            );
          })}

          {phase === "running" && !reduced && (
            /* Ambient: one unhurried pass across the forecast region, seam-free
               because it fades up after it enters and down before it leaves, so
               the wrap back to the now-line is never visible. Linear on purpose
               — an eased traverse spends its slow ends where the sweep is
               already invisible and its fast middle where it is not, which
               reads as a dart rather than a drift. The soft edge is two
               coincident strokes rather than a gradient, which would need a
               document-unique id in `defs` for a halo eight pixels wide. */
            <motion.g
              animate={{
                x: [0, HORIZON_SPAN * 0.12, HORIZON_SPAN * 0.72, HORIZON_SPAN],
                opacity: [0, 1, 1, 0],
              }}
              transition={{
                duration: DURATION.ambient,
                times: [0, 0.12, 0.72, 1],
                repeat: Infinity,
                ease: "linear",
              }}
            >
              <line
                x1={NOW_X}
                y1={16}
                x2={NOW_X}
                y2={108}
                stroke="currentColor"
                strokeWidth={8}
                opacity={0.08}
              />
              <line
                x1={NOW_X}
                y1={16}
                x2={NOW_X}
                y2={108}
                stroke="currentColor"
                strokeWidth={1.2}
                opacity={0.4}
              />
            </motion.g>
          )}

          <text x={X0} y={126} fill="currentColor" fontSize={8} opacity={0.65}>
            observed history
          </text>
          <text x={NOW_X + 6} y={126} fill="currentColor" fontSize={8} opacity={0.65}>
            forecast horizon
          </text>
        </g>
      </svg>
    </div>
  );
}
