/**
 * neural_net's signature: activations forward, gradients back, over a two-tier
 * epoch/batch ladder.
 *
 * No design doc exists for this model, so it is derived from the same
 * principle the others follow and from what `job/models/neural_net/model.py`
 * actually does.
 *
 * Two halves, for two different jobs:
 *
 *  - **The layer sketch** does the recognition job — this is the one model on
 *    the platform that genuinely is a feed-forward network, so a nodes-and-
 *    edges figure describes it rather than decorating it. (Which is precisely
 *    the argument against putting the same figure on `forecasting`, where the
 *    model is `SGDRegressor.partial_fit` and the wireframe carries a callout
 *    saying "not a neural net".) The counts at the two ENDS are real: four
 *    inputs and three classes are compile-time constants of the model. The
 *    two middle columns are not — `hidden` is config and never reaches the
 *    payload, so the widths drawn are a sketch, and the honesty note says so.
 *
 *  - **The ladder** does the state job, and it is the thing that makes this
 *    model's telemetry visible: the top tier is one cell per epoch, filled
 *    from `level: "epoch"` messages; the bottom tier is the position of the
 *    current batch inside the current epoch, from `level: "batch"` ones. Two
 *    levels arrive interleaved on one stream, and two tiers is the honest way
 *    to draw that. Both are real.
 *
 * Nothing here encodes accuracy. Accuracy on this problem means nothing
 * without the majority-class baseline beside it, and that is the chart's job.
 *
 * ## The lifecycle phases, per `motion.ts`
 *
 *   idle      the network drawn cold — every column and every edge band at a
 *             fraction of its working weight — over an empty ladder. Nothing
 *             moves. QUEUED lands here because `phaseOf` says so: a run still
 *             waiting for compute has done no forward passes, and it used to
 *             get the same sweep as a run that was actually training.
 *   starting  the network warms from the input end to the output end, ONCE,
 *             over DURATION.inhale, and then holds. It does not loop and does
 *             not fade back out — a cold Databricks job sits in this frame for
 *             tens of seconds, and a pulse here would draw waiting as work.
 *   running   one training step per DURATION.ambient: a wave of activation
 *             across the four columns, then the three edge bands lighting in
 *             reverse as the gradients come back.
 *   settled   one flat frame at the terminal colour. Nothing pulses, nothing
 *             drifts, and the ladder stops reporting a batch in flight.
 *
 * ## Why the two legs use different elements
 *
 * The legs have to be told apart at a glance or the loop reads as an aimless
 * shimmer — which is what brightening the same elements in both directions
 * looked like. So activations light the NODES and gradients light the EDGES,
 * and that split is the true one rather than a convenience: activations are
 * values at units, gradients are with respect to weights, and the weights are
 * what the edges are.
 *
 * The cadence is fixed, not one pulse per real batch — see the view's honesty
 * note. Tying it to messages was considered and dropped: this model emits a
 * couple of batch samples per epoch, so a message-driven pulse would fire in
 * bursts separated by dead air, and the phase it has to hold longest is the
 * one it would say least in.
 *
 * The thing this replaces was a single SMIL `<rect>` sweeping left to right on
 * an invented 1.9s cadence, which ran in QUEUED as well and said nothing about
 * a network in particular.
 */

import { motion, type Transition } from "motion/react";

import type { UiRunState } from "@/lib/envelope";
import { NEURAL_NET_CLASS_LABELS, NEURAL_NET_FEATURE_NAMES } from "@/lib/models";
import type { ModelViewProps } from "../contract";
import { DURATION, EASE, type MotionPhase, phaseOf } from "../motion";
import { usePrefersReducedMotion } from "../useReducedMotion";
import { trainingSummary } from "./series";

const NET_TOP = 14;
const NET_BOTTOM = 98;

function spread(count: number): number[] {
  if (count <= 1) return [(NET_TOP + NET_BOTTOM) / 2];
  const span = NET_BOTTOM - NET_TOP;
  return Array.from({ length: count }, (_, i) => NET_TOP + (i / (count - 1)) * span);
}

/**
 * The outer two layers are real: `FEATURE_NAMES` and `CLASS_LABELS` in
 * `job/models/neural_net/model.py` are fixed-length tuples, mirrored in
 * `@/lib/models` so the counts here are derived rather than retyped. The 6
 * and 5 are a sketch — the default `hidden` is [32, 16], which cannot be
 * drawn, and the actual widths are config the payload never carries.
 */
const LAYERS: readonly { x: number; ys: number[]; real: boolean }[] = [
  { x: 30, ys: spread(NEURAL_NET_FEATURE_NAMES.length), real: true },
  { x: 126, ys: spread(6), real: false },
  { x: 222, ys: spread(5), real: false },
  { x: 316, ys: spread(NEURAL_NET_CLASS_LABELS.length), real: true },
];

function nodeRadius(real: boolean): number {
  return real ? 3.6 : 2.8;
}

interface Edge {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

/** Edges grouped by the gap they span, rather than one flat list: the backward
 *  pass lights one gap at a time, so the gap is the unit that animates. */
const EDGE_BANDS: readonly (readonly Edge[])[] = LAYERS.flatMap((layer, index) => {
  const next = LAYERS[index + 1];
  if (next === undefined) return [];
  return [layer.ys.flatMap((y1) => next.ys.map((y2) => ({ x1: layer.x, y1, x2: next.x, y2 })))];
});

const LADDER_X0 = 10;
const LADDER_X1 = 390;
const LADDER_Y = 122;
const LADDER_H = 14;
const BATCH_Y = LADDER_Y + LADDER_H + 5;
const BATCH_H = 5;
/** Above this many epochs the cells are thinner than the gaps between them,
 *  so the ladder becomes one bar. `epochs` defaults to 12; 48 is generous. */
const MAX_CELLS = 48;

/* --- timing ---------------------------------------------------------------
 *
 * Every number below is a fraction of a duration from `motion.ts`. Nothing
 * here is picked by feel.
 */

/** One training step — out and back — per cycle. Each leg is then a little
 *  over a second, which on its own would sit under the floor `motion.ts`
 *  warns about; the repeating unit the eye actually picks up here is the round
 *  trip, and a round trip stretched over two ambient cycles stops reading as
 *  one gesture and starts reading as two unrelated ones. */
const CYCLE = DURATION.ambient;

/** A pulse rises, falls, and then keeps out of the way for the rest of the
 *  cycle. Fractions of CYCLE, so the shape holds if the cycle is retuned. */
const PULSE_WIDTH = 0.16;
const PULSE_TIMES = [0, 0.06, PULSE_WIDTH, 1];

/** How much of the cycle a wave takes to cross the network. */
const WAVE_SPAN = 0.35;

/**
 * Gap between one element lighting and the next.
 *
 * Deliberately not `staggerFor`. That exists to CAP a stagger so a data-driven
 * fill cannot end up describing the past; this wave carries no data and its
 * whole job is to take a legible amount of time. `staggerFor(4)` would cross
 * the network in 0.12s, which is the strobe.
 */
const FORWARD_STEP = (CYCLE * WAVE_SPAN) / (LAYERS.length - 1);
const BACK_STEP = (CYCLE * WAVE_SPAN) / (EDGE_BANDS.length - 1);

/** Placed so the last gradient pulse finishes exactly as the cycle does. Each
 *  element loops on its own clock, so one still mid-pulse when its cycle
 *  restarts snaps back to rest in a single frame — a visible tick, once every
 *  2.4s, forever. */
const BACK_START = CYCLE * (1 - WAVE_SPAN - PULSE_WIDTH);

/** The inhale crosses the same stages the network is drawn in — column, band,
 *  column, ... — with the step sized so the LAST stage finishes at
 *  DURATION.inhale rather than the first one starting there. */
const INHALE_STAGES = LAYERS.length * 2 - 1;
const INHALE_STEP = (DURATION.inhale - DURATION.slow) / (INHALE_STAGES - 1);

/**
 * Group opacity per phase. The per-node weights inside a column (0.9 for the
 * real counts, 0.4 for the sketch) multiply through this, so the one thing the
 * picture encodes survives every phase rather than being flattened by the
 * wash.
 */
const NODE_WASH: Record<MotionPhase, number> = {
  idle: 0.45,
  starting: 1,
  running: 1,
  settled: 0.9,
};

/** Edges carry their weight on the group rather than per line. At 0.2 each,
 *  sixty-nine overlapping strokes compound into a dark smear exactly where the
 *  sketch columns are — the part of the figure that should recede. */
const EDGE_WASH: Record<MotionPhase, number> = {
  idle: 0.07,
  starting: 0.2,
  running: 0.2,
  settled: 0.13,
};

/** Both pulses ride on the same `opacity` the wash uses, with the resting
 *  value baked into the first and last keyframes. A second animation on one
 *  property would simply win, and the alternative — a second copy of all
 *  sixty-nine edge lines to glow on top of the first — is a lot of DOM for one
 *  highlight. */
const EDGE_PULSE = [EDGE_WASH.running, 0.5, EDGE_WASH.running, EDGE_WASH.running];
const HALO_PULSE = [0, 0.38, 0, 0];

const TONE: Record<UiRunState, string> = {
  STARTING: "text-accent",
  QUEUED: "text-info",
  RUNNING: "text-info",
  SUCCEEDED: "text-good",
  FAILED: "text-bad",
  CANCELLED: "text-idle",
  INFEASIBLE: "text-warn",
};

export function NeuralNetSignature({ state, snapshot }: ModelViewProps) {
  const reduced = usePrefersReducedMotion();
  const phase = phaseOf(state);
  const settled = phase === "settled";

  const { latest, epochPoints, epochsTotal, batchesPerEpoch } = trainingSummary(
    snapshot.progress,
  );

  // Completed epochs come from the EPOCH-level messages only. A batch-level
  // message carries the epoch it is inside, which is not a completed one —
  // reading `latest.epoch` here would fill a cell two thirds of an epoch
  // early, every epoch.
  const lastEpochPoint = epochPoints.at(-1);
  const completedEpochs =
    state === "SUCCEEDED" && epochsTotal !== null
      ? epochsTotal
      : lastEpochPoint?.epoch !== null && lastEpochPoint?.epoch !== undefined
        ? lastEpochPoint.epoch + 1
        : 0;

  // Batch position inside the epoch now in flight. Only meaningful while the
  // run is moving; a finished run's last partial epoch is not "in progress".
  const batchFraction =
    !settled && latest !== null && latest.batch !== null && batchesPerEpoch !== null && batchesPerEpoch > 0
      ? Math.min(1, (latest.batch + 1) / batchesPerEpoch)
      : null;

  const cells = epochsTotal !== null && epochsTotal > 0 && epochsTotal <= MAX_CELLS
    ? epochsTotal
    : null;

  const tone = state === null ? "text-faint" : TONE[state];
  // Everything ambient hangs off this one flag, so there is exactly one place
  // where "the run is working" and "the user wants motion" are combined.
  const propagating = phase === "running" && !reduced;

  /** The phase wash: one slow pass along the network during `starting`, a
   *  plain state change everywhere else. `stage` is the element's position in
   *  that pass, counting column, band, column, ... from the input end. */
  const wash = (stage: number): Transition => {
    if (reduced) return { duration: 0 };
    if (phase === "starting") {
      return { duration: DURATION.slow, ease: EASE.decelerate, delay: stage * INHALE_STEP };
    }
    // Settling gets the longer curve: it is the one moment the whole figure
    // changes at once, and DURATION.base makes that read as a glitch.
    return { duration: settled ? DURATION.slow : DURATION.base, ease: EASE.standard };
  };

  const pulse = (delay: number): Transition => ({
    duration: CYCLE,
    times: PULSE_TIMES,
    delay,
    repeat: Infinity,
    ease: EASE.standard,
  });

  /** Leaving `running` for any reason: fade the glow out rather than cutting
   *  it, so a terminal message does not land as a blink. */
  const glowOff: Transition = { duration: reduced ? 0 : DURATION.base, ease: EASE.standard };

  const label =
    state === null
      ? "No run selected. Network idle."
      : `${state.toLowerCase()} — ${completedEpochs}${
          epochsTotal !== null ? ` of ${epochsTotal}` : ""
        } epochs complete` +
        (batchFraction !== null && latest?.batch !== null && latest?.batch !== undefined
          ? `, batch ${latest.batch + 1} of ${batchesPerEpoch ?? "?"} in the current epoch`
          : "");

  return (
    // No card chrome: the page owns layout, and a border drawn by both would
    // double up.
    <div className="w-full">
      <svg viewBox="0 0 400 152" className="h-auto w-full" role="img" aria-label={label}>
        <title>{label}</title>
        {/* The terminal state is one flat wash over the whole visual rather
            than a per-element recolour, so the tone lives here and everything
            below is `currentColor`. The duration is set from the vocabulary
            rather than picked off Tailwind's `duration-*` scale so the colour
            and the opacity halves of the same settle land together; Tailwind's
            default timing function is already EASE.standard. */}
        <g
          className={`transition-colors motion-reduce:transition-none ${tone}`}
          style={{ transitionDuration: `${DURATION.slow}s` }}
        >
          {/* --- the network ------------------------------------------- */}
          {/* Edge bands, back to front: mesh, then the activation glow, then
              the nodes, so the nodes stay crisp on top of their own halo. */}
          {EDGE_BANDS.map((band, index) => (
            <motion.g
              key={`band-${index}`}
              stroke="currentColor"
              strokeWidth={0.5}
              // Absent from `initial` unless this is a mount straight into
              // STARTING — a run opened from history is already finished, and
              // warming its network would animate the past.
              initial={phase === "starting" && !reduced ? { opacity: EDGE_WASH.idle } : false}
              animate={{ opacity: propagating ? EDGE_PULSE : EDGE_WASH[phase] }}
              transition={
                propagating
                  ? pulse(BACK_START + (EDGE_BANDS.length - 1 - index) * BACK_STEP)
                  : wash(index * 2 + 1)
              }
            >
              {band.map((edge, i) => (
                <line key={i} x1={edge.x1} y1={edge.y1} x2={edge.x2} y2={edge.y2} />
              ))}
            </motion.g>
          ))}

          {LAYERS.map((layer, index) => (
            <motion.g
              key={`halo-${layer.x}`}
              fill="currentColor"
              initial={false}
              animate={{ opacity: propagating ? HALO_PULSE : 0 }}
              transition={propagating ? pulse(index * FORWARD_STEP) : glowOff}
            >
              {layer.ys.map((y) => (
                <circle key={y} cx={layer.x} cy={y} r={nodeRadius(layer.real) + 4.4} />
              ))}
            </motion.g>
          ))}

          {LAYERS.map((layer, index) => (
            <motion.g
              key={layer.x}
              initial={phase === "starting" && !reduced ? { opacity: NODE_WASH.idle } : false}
              animate={{ opacity: NODE_WASH[phase] }}
              transition={wash(index * 2)}
            >
              {layer.ys.map((y) => (
                <circle
                  key={y}
                  cx={layer.x}
                  cy={y}
                  r={nodeRadius(layer.real)}
                  fill="currentColor"
                  // The ends are real counts, the middle is a sketch, and the
                  // weight difference is the visual cue for that.
                  opacity={layer.real ? 0.9 : 0.4}
                />
              ))}
            </motion.g>
          ))}

          {NEURAL_NET_CLASS_LABELS.map((className, index) => {
            const y = LAYERS[LAYERS.length - 1]?.ys[index];
            if (y === undefined) return null;
            return (
              <text
                key={className}
                x={328}
                y={y + 3}
                fill="currentColor"
                fontSize={8}
                opacity={0.7}
              >
                {className}
              </text>
            );
          })}

          <text x={LADDER_X0} y={110} fill="currentColor" fontSize={8} opacity={0.6}>
            4 basis features from trip_distance
          </text>
          {/* Names the direction the loop is describing. Static, and phrased
              about the network rather than about this run, because the cadence
              is fixed — see the header and the honesty note. */}
          <text x={LADDER_X1} y={110} textAnchor="end" fill="currentColor" fontSize={8} opacity={0.6}>
            activations forward, gradients back
          </text>

          {/* --- epoch ladder ------------------------------------------- */}
          {/* The data half. No wash, no pulse, no stagger: it reports rather
              than breathes, and a cell that fills late is a cell describing
              the past. With `initial={false}` a backfilled run renders its
              cells already filled, which is the case a stagger would have
              been for. */}
          {cells !== null ? (
            Array.from({ length: cells }, (_, index) => {
              const gap = 2;
              const width = (LADDER_X1 - LADDER_X0 - gap * (cells - 1)) / cells;
              const x = LADDER_X0 + index * (width + gap);
              const done = index < completedEpochs;
              const current = index === completedEpochs && !settled;
              return (
                <g key={index}>
                  <motion.rect
                    x={x}
                    y={LADDER_Y}
                    width={Math.max(1, width)}
                    height={LADDER_H}
                    rx={2}
                    fill="currentColor"
                    initial={false}
                    animate={{ opacity: done ? (settled ? 0.85 : 1) : current ? 0.35 : 0.14 }}
                    transition={
                      reduced ? { duration: 0 } : { duration: DURATION.base, ease: EASE.standard }
                    }
                  />
                  {current && batchFraction !== null && (
                    <motion.rect
                      x={x}
                      y={BATCH_Y}
                      height={BATCH_H}
                      rx={1}
                      fill="currentColor"
                      opacity={0.95}
                      initial={false}
                      animate={{ width: Math.max(0.5, width * batchFraction) }}
                      // A tween, not SPRING: the marker's full width is the
                      // cell's width, and a spring's overshoot puts it past
                      // the cell edge on the last batch of every epoch.
                      transition={
                        reduced ? { duration: 0 } : { duration: DURATION.base, ease: EASE.standard }
                      }
                    />
                  )}
                </g>
              );
            })
          ) : (
            // No epochs_total yet, or more epochs than cells worth drawing.
            // One bar, filled by whatever fraction is actually known.
            <>
              <rect
                x={LADDER_X0}
                y={LADDER_Y}
                width={LADDER_X1 - LADDER_X0}
                height={LADDER_H}
                rx={3}
                fill="currentColor"
                opacity={0.14}
              />
              <motion.rect
                x={LADDER_X0}
                y={LADDER_Y}
                height={LADDER_H}
                rx={3}
                fill="currentColor"
                opacity={settled ? 0.85 : 1}
                initial={false}
                animate={{
                  width:
                    ((LADDER_X1 - LADDER_X0) *
                      Math.min(100, Math.max(0, latest?.percent ?? 0))) /
                    100,
                }}
                transition={
                  reduced ? { duration: 0 } : { duration: DURATION.base, ease: EASE.standard }
                }
              />
            </>
          )}

          <text x={LADDER_X0} y={148} fill="currentColor" fontSize={8} opacity={0.6}>
            epochs
          </text>
          <text x={LADDER_X1} y={148} textAnchor="end" fill="currentColor" fontSize={8} opacity={0.6}>
            batches within the current epoch
          </text>
        </g>
      </svg>
    </div>
  );
}
