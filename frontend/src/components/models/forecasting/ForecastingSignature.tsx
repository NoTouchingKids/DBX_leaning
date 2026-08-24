/**
 * Forecasting's signature: a horizon timeline that draws in point by point.
 *
 * ## Why this and not the neural net the design digest sketches
 *
 * The digest's own table flags the Three.js neural net as an **open question,
 * not a decision** — because `models/forecasting/model.py` is
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
 * ## What is real (mirrored in the view's `honesty` note)
 *
 * Real: how many horizon markers are lit, which tracks `percent_complete`
 * (`100 * (epoch + 1) / epochs`, populated on every message for this model),
 * and the single flat terminal colour.
 *
 * Decorative: the history waveform, the projected path the markers sit on,
 * and the width of the band. The band is a drawing device. This model emits
 * no prediction interval of any kind, and a ribbon that narrows is the single
 * easiest thing on the page to misread as one — hence the caption, and hence
 * the fact that it narrows with *elapsed progress* rather than with any
 * measure of uncertainty.
 */

import type { UiRunState } from "@/lib/envelope";
import { isAnimating, isSettled, payloadOf, type ModelViewProps } from "../contract";
import type { ForecastingProgressPayload } from "@/lib/models";
import { usePrefersReducedMotion } from "../useReducedMotion";

/* Geometry. One viewBox, scaled by CSS — the component fills the width it is
   given and sets its own height through the aspect ratio, per the contract. */
const X0 = 8;
const NOW_X = 148;
const X1 = 392;
const MID_Y = 68;
const HISTORY_STEPS = 40;
const HORIZON_MARKS = 24;

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

export function ForecastingSignature({ state, snapshot }: ModelViewProps) {
  const reduced = usePrefersReducedMotion();
  const settled = isSettled(state);
  const moving = isAnimating(state);

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
  const bandScale = 1 - 0.82 * convergence;

  const upper: string[] = [];
  const lower: string[] = [];
  for (let i = 0; i <= HORIZON_MARKS; i += 1) {
    const u = i === 0 ? NOW_U : horizonU(i - 1);
    const t = (u - NOW_U) / (1 - NOW_U);
    const half = 24 * t * bandScale;
    const x = xAt(u).toFixed(1);
    upper.push(`${x},${(wave(u) - half).toFixed(1)}`);
    lower.unshift(`${x},${(wave(u) + half).toFixed(1)}`);
  }
  const bandPath = `M ${upper.join(" L ")} L ${lower.join(" L ")} Z`;

  // Ambient sweep only while something is genuinely running with no
  // percentage to show. Reduced motion drops the sweep, never the markers —
  // the marker count is the information, the sweep is the transition.
  const sweeping = moving && !reduced && (fraction === null || state === "STARTING");

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
        <g className={state === null ? "text-faint" : TONE[state]}>
          {/* Baseline and the "now" divider. */}
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

          {/* The band. Hidden before anything starts so a queued run does not
              show a fan it has not earned. */}
          {state !== null && state !== "QUEUED" && (
            <path d={bandPath} fill="currentColor" opacity={0.09} />
          )}

          {/* Horizon markers, drawn in left to right. */}
          {Array.from({ length: HORIZON_MARKS }, (_, i) => {
            const u = horizonU(i);
            const on = i < lit;
            return (
              <circle
                key={i}
                cx={xAt(u)}
                cy={wave(u)}
                r={on ? 3 : 1.8}
                fill="currentColor"
                className="transition-opacity duration-500 motion-reduce:transition-none"
                style={{ opacity: on ? (settled ? 0.9 : 1) : 0.18 }}
              />
            );
          })}

          {sweeping && (
            <line
              x1={NOW_X}
              y1={16}
              x2={NOW_X}
              y2={108}
              stroke="currentColor"
              strokeWidth={1.5}
            >
              <animate
                attributeName="x1"
                values={`${NOW_X};${X1}`}
                dur="2.6s"
                repeatCount="indefinite"
              />
              <animate
                attributeName="x2"
                values={`${NOW_X};${X1}`}
                dur="2.6s"
                repeatCount="indefinite"
              />
              <animate
                attributeName="opacity"
                values="0;0.55;0"
                dur="2.6s"
                repeatCount="indefinite"
              />
            </line>
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
