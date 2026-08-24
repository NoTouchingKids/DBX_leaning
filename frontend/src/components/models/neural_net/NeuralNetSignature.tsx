/**
 * neural_net's signature: a layer sketch over a two-tier epoch/batch ladder.
 *
 * No design doc exists for this model, so it is derived from the same
 * principle the others follow and from what `models/neural_net/model.py`
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
 */

import type { UiRunState } from "@/lib/envelope";
import { NEURAL_NET_CLASS_LABELS } from "@/lib/models";
import { isAnimating, isSettled, type ModelViewProps } from "../contract";
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
 * 4 in, 3 out are real: `FEATURE_NAMES` and `CLASS_LABELS` in
 * `models/neural_net/model.py` are fixed-length tuples. The 6 and 5 are a
 * sketch — the default `hidden` is [32, 16], which cannot be drawn, and the
 * actual widths are config the payload never carries.
 */
const LAYERS: readonly { x: number; ys: number[]; real: boolean }[] = [
  { x: 30, ys: spread(4), real: true },
  { x: 126, ys: spread(6), real: false },
  { x: 222, ys: spread(5), real: false },
  { x: 316, ys: spread(NEURAL_NET_CLASS_LABELS.length), real: true },
];

const EDGES = LAYERS.flatMap((layer, index) => {
  const next = LAYERS[index + 1];
  if (next === undefined) return [];
  return layer.ys.flatMap((y1) => next.ys.map((y2) => ({ x1: layer.x, y1, x2: next.x, y2 })));
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
  const settled = isSettled(state);
  const moving = isAnimating(state);

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
  // The forward-pass sweep is pacing, not measurement — it runs at a fixed
  // cadence while the run is moving and stops dead when it settles.
  const sweeping = moving && !reduced;

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
        <g className={tone}>
          {EDGES.map((edge, index) => (
            <line
              key={index}
              x1={edge.x1}
              y1={edge.y1}
              x2={edge.x2}
              y2={edge.y2}
              stroke="currentColor"
              strokeWidth={0.5}
              opacity={settled ? 0.12 : 0.18}
            />
          ))}

          {LAYERS.map((layer) =>
            layer.ys.map((y) => (
              <circle
                key={`${layer.x}-${y}`}
                cx={layer.x}
                cy={y}
                r={layer.real ? 3.6 : 2.8}
                fill="currentColor"
                // The ends are real counts, the middle is a sketch, and the
                // weight difference is the visual cue for that.
                opacity={layer.real ? 0.9 : 0.4}
              />
            )),
          )}

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

          {sweeping && (
            <rect x={16} y={NET_TOP - 8} width={10} height={NET_BOTTOM - NET_TOP + 16} fill="currentColor" opacity={0}>
              <animate attributeName="x" values="16;312" dur="1.9s" repeatCount="indefinite" />
              <animate attributeName="opacity" values="0;0.22;0" dur="1.9s" repeatCount="indefinite" />
            </rect>
          )}

          {/* --- epoch ladder ------------------------------------------- */}
          {cells !== null ? (
            Array.from({ length: cells }, (_, index) => {
              const gap = 2;
              const width = (LADDER_X1 - LADDER_X0 - gap * (cells - 1)) / cells;
              const x = LADDER_X0 + index * (width + gap);
              const done = index < completedEpochs;
              const current = index === completedEpochs && !settled;
              return (
                <g key={index}>
                  <rect
                    x={x}
                    y={LADDER_Y}
                    width={Math.max(1, width)}
                    height={LADDER_H}
                    rx={2}
                    fill="currentColor"
                    className="transition-opacity duration-300 motion-reduce:transition-none"
                    style={{ opacity: done ? (settled ? 0.85 : 1) : current ? 0.35 : 0.14 }}
                  />
                  {current && batchFraction !== null && (
                    <rect
                      x={x}
                      y={BATCH_Y}
                      width={Math.max(0.5, width * batchFraction)}
                      height={BATCH_H}
                      rx={1}
                      fill="currentColor"
                      opacity={0.95}
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
              <rect
                x={LADDER_X0}
                y={LADDER_Y}
                width={
                  ((LADDER_X1 - LADDER_X0) *
                    Math.min(100, Math.max(0, latest?.percent ?? 0))) /
                  100
                }
                height={LADDER_H}
                rx={3}
                fill="currentColor"
                className="transition-[width] duration-300 motion-reduce:transition-none"
                opacity={settled ? 0.85 : 1}
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
