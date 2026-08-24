/**
 * The cooling lattice — annealing's signature animation.
 *
 * A field of cells that flip between taken and not taken. Which cells are lit
 * is decorative and always will be: the progress payload carries `iteration`,
 * `temperature`, an objective and a weight, and nothing per-trip, so there is
 * no selection to draw. What is real is the PACE and the COLOUR, both derived
 * from the run's own `temperature` normalised against its cooling schedule
 * (`deriveHeat`) — a hot search shimmers in warm tones and changes several
 * cells a second, a cold one is nearly still and blue. That is the annealing
 * intuition itself, and it is the hook `contract.ts` asks for: pacing tracks
 * something real, positions do not.
 *
 * The lattice is the only place a viewer sees the run's temperature without
 * reading a number, which is why the readout under it names the same value —
 * a claim that the animation tracks something real needs the receipt visible.
 *
 * `feasible: false` gets no alarm colour anywhere in here. The search leaves
 * the feasible region on purpose (the model prices overweight rather than
 * forbidding it, so a full knapsack is escapable), the incumbent it reports is
 * feasible regardless, and `ShiftUsage.tone` is typed so a future edit cannot
 * quietly make it red.
 */

import { useEffect, useMemo, useState } from "react";

import { isAnimating, type ModelViewProps } from "@/components/models/contract";
import { usePrefersReducedMotion } from "@/components/models/useReducedMotion";
import { EMPTY } from "@/lib/format";

import {
  frameFor,
  heatCellClass,
  heatMotion,
  RESTING_CELL_CLASS,
} from "./frames";
import {
  buildPoints,
  deriveHeat,
  heatPhase,
  HEAT_PHASE_TEXT,
  shiftUsage,
} from "./series";

const COLUMNS = 20;
const ROWS = 8;
const CELL_COUNT = COLUMNS * ROWS;

/** A fixed, deterministic starting pattern — and the whole pattern under
 *  reduced motion. Deterministic so the lattice does not reshuffle on every
 *  re-render, which at ~1 render per progress message would read as churn the
 *  run did not cause. */
function restingPattern(): boolean[] {
  return Array.from({ length: CELL_COUNT }, (_, i) => (i * 37) % 11 < 4);
}

function churn(previous: readonly boolean[], flips: number): boolean[] {
  const next = previous.slice();
  for (let k = 0; k < flips; k += 1) {
    const i = Math.floor(Math.random() * next.length);
    next[i] = next[i] !== true;
  }
  return next;
}

const SIG_FMT = new Intl.NumberFormat(undefined, { maximumSignificantDigits: 3 });

function formatTemperature(value: number | null): string {
  return value === null ? EMPTY : SIG_FMT.format(value);
}

function formatRate(value: number | null): string {
  return value === null ? EMPTY : `${Math.round(value * 100)}%`;
}

function formatInt(value: number | null | undefined): string {
  return value === null || value === undefined ? EMPTY : String(Math.round(value));
}

export function AnnealingSignature({ state, snapshot }: ModelViewProps) {
  const reducedMotion = usePrefersReducedMotion();
  const frame = frameFor(state);

  const points = useMemo(() => buildPoints(snapshot.progress), [snapshot.progress]);
  const latest = points.at(-1) ?? null;
  const heat = deriveHeat(points);
  const phase = heatPhase(heat);
  const usage = shiftUsage(latest);
  const pace = heatMotion(heat);

  const [lit, setLit] = useState<boolean[]>(restingPattern);

  // Reduced motion keeps the colour and the phase — hot versus cold survives —
  // and drops only the churn. The information is in the palette and the
  // readout, not in the movement. `frame.animated` is false in every terminal
  // state, so a settled run freezes here for free.
  const churning = frame.animated && !reducedMotion;

  useEffect(() => {
    if (!churning) return;
    const id = setInterval(() => {
      setLit((previous) => churn(previous, pace.flipsPerTick));
    }, pace.intervalMs);
    return () => clearInterval(id);
  }, [churning, pace.flipsPerTick, pace.intervalMs]);

  const litClass = frame.heated ? heatCellClass(heat) : frame.cellClass;
  // One flat frame for the whole lattice once the run is over, per the
  // contract: no cell means anything different from any other cell at that
  // point, so the lit/unlit split stops being drawn at all.
  const flat = !frame.heated;

  return (
    <div>
      <div className="mb-3 flex items-start gap-2.5">
        <span
          // The dot pulses through QUEUED and STARTING too, where the lattice
          // has nothing to say yet — a cold job start on Databricks is tens of
          // seconds, and a page with nothing moving at all in it reads as
          // broken. `.live-dot` is already silenced under reduced motion by
          // `index.css`.
          className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${frame.dotClass} ${
            isAnimating(state) ? "live-dot" : ""
          }`}
        />
        <div>
          <div className="text-[0.86rem] font-semibold">{frame.headline}</div>
          <div className="text-[0.72rem] text-dim">
            {frame.heated
              ? HEAT_PHASE_TEXT[phase]
              : "simulated annealing over a shift of trips"}
          </div>
        </div>
      </div>

      <div
        aria-hidden="true"
        className="grid h-[132px] gap-[3px] rounded-[8px] border border-dashed border-edge bg-paper p-2"
        style={{
          gridTemplateColumns: `repeat(${COLUMNS}, minmax(0, 1fr))`,
          gridAutoRows: "1fr",
        }}
      >
        {lit.map((on, index) => (
          <span
            key={index}
            className={`rounded-[2px] border ${
              flat || on ? litClass : RESTING_CELL_CLASS
            } ${reducedMotion ? "" : "transition-colors"}`}
            style={
              reducedMotion
                ? undefined
                : // Snappy when hot, languid when cold: the transition length
                  // is the second half of the cooling signal, and it is why a
                  // cold lattice reads as settling rather than as stalled.
                  { transitionDuration: `${pace.transitionMs}ms` }
            }
          />
        ))}
      </div>

      <dl className="mt-3 flex flex-wrap gap-x-6 gap-y-2 text-[0.7rem]">
        <Readout
          term="temperature"
          value={formatTemperature(latest?.temperature ?? null)}
        />
        <Readout
          term="acceptance rate"
          value={formatRate(latest?.acceptanceRate ?? null)}
        />
        <Readout
          term="shift used"
          value={usage?.label ?? EMPTY}
          // `info` for an over-capacity walk, never `bad` or `warn` — see
          // `CalmTone`. Being over the shift mid-search is the algorithm, and
          // the objective already prices it.
          tone={usage?.overShift === true ? "text-info" : undefined}
          detail={usage?.overShift === true ? usage.note : undefined}
        />
        <Readout
          term="trips in best shift"
          value={formatInt(latest?.itemsSelected)}
          // Spelled out because this is the one payload key describing the
          // incumbent rather than the walk, and pairing it with the current
          // numbers above would be reading two solutions as one.
          detail="from the best feasible selection, not the current walk"
        />
      </dl>
    </div>
  );
}

function Readout({
  term,
  value,
  tone,
  detail,
}: {
  term: string;
  value: string;
  tone?: string;
  detail?: string;
}) {
  return (
    <div className="min-w-[6.5rem]" title={detail}>
      <dt className="font-mono text-[0.64rem] text-faint">{term}</dt>
      <dd className={`font-mono text-[0.82rem] ${tone ?? "text-ink"}`}>{value}</dd>
      {detail !== undefined && (
        <p className="mt-0.5 max-w-[24ch] text-[0.62rem] leading-snug text-faint">
          {detail}
        </p>
      )}
    </div>
  );
}
