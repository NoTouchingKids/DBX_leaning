/**
 * The cooling lattice — annealing's signature animation.
 *
 * A field of cells that flip between taken and not taken, above a block of
 * settled ones that rises from the bottom as the search cools. Which cells are
 * lit is decorative and always will be: the progress payload carries
 * `iteration`, `temperature`, an objective and a weight, and nothing per-trip,
 * so there is no selection to draw. What is real is the EXTENT of the motion
 * and the COLOUR, both derived from the run's own `temperature` normalised
 * against its cooling schedule (`deriveHeat`) — a hot search agitates the whole
 * field in warm tones, a cold one has crystallised almost to the top and
 * changes a single cell at a time in blue. That is the annealing intuition
 * itself, and it is the hook `contract.ts` asks for: extent and palette track
 * something real, positions do not.
 *
 * ## Heat is extent, not rate — the previous version had this backwards
 *
 * The first build read the temperature as a flicker rate: `heatMotion` still
 * returns an `intervalMs` that runs from 110ms hot to 1400ms cold, and the
 * lattice churned on it. Two things were wrong with that, and both are the
 * reason nothing here reads `intervalMs` any more.
 *
 * A 110ms cycle is a strobe. `motion.ts` puts the floor for an ambient loop at
 * around 1.5s for exactly this reason: this panel has to be watchable for the
 * ten minutes a real solve takes, and nine changes a second is not something
 * anyone can sit next to. And the rate was never delivered anyway — the
 * interval was re-created on every dependency change, which meant every
 * progress message, about once a second, so a cold lattice on a 1400ms clock
 * was reset before it could ever fire. It looked frozen while claiming to be
 * paced.
 *
 * So the cadence is now constant at `DURATION.ambient`, one batch of proposals
 * per cycle, and the temperature is carried by how MUCH changes rather than how
 * often: how many cells turn over in a batch, how deep the settled block has
 * grown, and how warm the palette is. All three fall together as the run cools,
 * which is the thing worth seeing from across a room.
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

import { motion } from "motion/react";
import { useEffect, useMemo, useRef, useState } from "react";

import { isAnimating, type ModelViewProps } from "@/components/models/contract";
import {
  DURATION,
  EASE,
  phaseOf,
  staggerFor,
} from "@/components/models/motion";
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

/** One batch of proposals per ambient cycle, whatever the temperature. */
const CYCLE_MS = Math.round(DURATION.ambient * 1000);

/** How much of the still-agitated band turns over in one batch at full heat.
 *  Above about a quarter the field reads as noise rather than as a search. */
const FULL_HEAT_TURNOVER = 0.22;

/** `heatMotion`'s flip count at maximum heat, asked for rather than restated:
 *  it is the ramp `frames.ts` owns, and retuning it there should move the
 *  turnover here with it rather than silently rescaling this. */
const FULL_HEAT_FLIPS = heatMotion(1).flipsPerTick;

/** The settled block. `heatCellClass(0)` rather than a fourth colour, so the
 *  crystallised part is the cold end of the one ramp the lattice already
 *  uses — at which point, at the cold end, block and band converge and the
 *  whole grid reads as having stopped. Which it very nearly has. */
const SETTLED_CELL_CLASS = heatCellClass(0);

/**
 * A fixed scatter delay per cell, in milliseconds.
 *
 * `staggerFor` caps the step so the whole grid finishes inside
 * `STAGGER.budget` — a blind 40ms per cell over 160 of them would take 6.4s,
 * nearly three batches, and the ripple would still be arriving when the next
 * one started. The order is a fixed permutation rather than a left-to-right
 * sweep because a directional ripple every 2.4s reads as a scanner, which is
 * the wrong idea entirely for a stochastic search.
 */
const CELL_DELAY_MS: readonly number[] = (() => {
  const step = staggerFor(CELL_COUNT) * 1000;
  // 61 is coprime with 160, so this hits every rank exactly once.
  return Array.from({ length: CELL_COUNT }, (_, index) =>
    Math.round(step * ((index * 61) % CELL_COUNT)),
  );
})();

/** The starting inhale: one slow swell from the bottom, then it holds. There
 *  is no data yet, so it is the whole of what this frame has to say. */
const INHALE_FROM = { opacity: 0.4, scaleY: 0.94 };
const INHALE_KEYFRAMES = { opacity: [0.4, 1], scaleY: [0.94, 1] };
const AT_REST = { opacity: 1, scaleY: 1 };

/** A fixed, deterministic starting pattern — and the whole pattern under
 *  reduced motion. Deterministic so the lattice does not reshuffle on every
 *  re-render, which at ~1 render per progress message would read as churn the
 *  run did not cause. */
function restingPattern(): boolean[] {
  return Array.from({ length: CELL_COUNT }, (_, i) => (i * 37) % 11 < 4);
}

/** Flip cells at random, but only inside the agitated band. The settled block
 *  at the bottom is by definition not moving; churning underneath it and then
 *  drawing over the result would burn work to display nothing. */
function churn(
  previous: readonly boolean[],
  flips: number,
  bandCells: number,
): boolean[] {
  const next = previous.slice();
  for (let k = 0; k < flips; k += 1) {
    const i = Math.floor(Math.random() * bandCells);
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
  const motionPhase = phaseOf(state);

  const points = useMemo(() => buildPoints(snapshot.progress), [snapshot.progress]);
  const latest = points.at(-1) ?? null;
  const heat = deriveHeat(points);
  const thermal = heatPhase(heat);
  const usage = shiftUsage(latest);
  const pace = heatMotion(heat);

  // Rows crystallised out, counted up from the bottom. Null heat is "running,
  // no temperature yet", and annealing starts hot — nothing has settled.
  //
  // Capped one row short of the full grid: a run that is still RUNNING must
  // not present the same picture as one that has finished, and a lattice with
  // no live row left is exactly that picture.
  const floorRows = frame.heated
    ? Math.min(ROWS - 1, Math.round((1 - (heat ?? 1)) * ROWS))
    : 0;
  const bandCells = (ROWS - floorRows) * COLUMNS;
  const flips = Math.max(
    1,
    Math.round(
      bandCells * FULL_HEAT_TURNOVER * (pace.flipsPerTick / FULL_HEAT_FLIPS),
    ),
  );

  const [lit, setLit] = useState<boolean[]>(restingPattern);

  // Reduced motion keeps the colour, the settled block and the phase — hot
  // versus cold survives, and so does how far through the schedule the run is —
  // and drops only the churn, which carries nothing the other two do not.
  // `frame.animated` is false in every terminal state, so a settled run freezes
  // here for free.
  const churning = frame.animated && !reducedMotion;

  // The batch clock must not depend on the pace. Heat moves on every progress
  // message, roughly once a second; putting `flips` in the dependency array
  // would tear down and re-create a 2.4s interval before it ever fired. That
  // is the bug the old 110ms clock was hiding, so the current pace is read
  // through a ref instead and the interval is created once per run.
  const paceRef = useRef({ flips, bandCells });
  useEffect(() => {
    paceRef.current = { flips, bandCells };
  });

  useEffect(() => {
    if (!churning) return;
    const tick = () => {
      const current = paceRef.current;
      setLit((previous) => churn(previous, current.flips, current.bandCells));
    };
    // Once immediately, so entering RUNNING is marked by a change rather than
    // by 2.4 seconds of the starting frame still sitting there.
    tick();
    const id = setInterval(tick, CYCLE_MS);
    return () => clearInterval(id);
  }, [churning]);

  const litClass = frame.heated ? heatCellClass(heat) : frame.cellClass;
  // One flat frame for the whole lattice once the run is over, per the
  // contract: no cell means anything different from any other cell at that
  // point, so neither the lit/unlit split nor the settled block is drawn.
  const flat = !frame.heated;
  const cellClassAt = (index: number): string => {
    if (flat) return litClass;
    if (index >= bandCells) return SETTLED_CELL_CLASS;
    return lit[index] === true ? litClass : RESTING_CELL_CLASS;
  };

  // Snappy when hot, languid when cold: the fade length is the last of the
  // cooling signals, and it is why a cold lattice reads as settling rather
  // than as stalled. Flat frames get the settling duration instead — the walk
  // from the running colours into the terminal one is a single gesture, and
  // then nothing moves again.
  const fadeMs = frame.heated
    ? pace.transitionMs
    : Math.round(DURATION.slow * 1000);

  const inhaling = motionPhase === "starting" && !reducedMotion;
  // Evaluated once. Mounting straight into STARTING — a page opened on a run
  // that was triggered somewhere else — should still get the inhale; mounting
  // into any other phase must not animate in at all, because a finished run
  // that fades up on arrival is motion after the end of the run.
  const [initialFrame] = useState<typeof INHALE_FROM | false>(() =>
    motionPhase === "starting" && !reducedMotion ? INHALE_FROM : false,
  );

  return (
    <div>
      <div className="mb-3 flex items-start gap-2.5">
        <span
          // The dot pulses through QUEUED as well, where the lattice is idle
          // and has nothing to say — a queued run is waiting on one of the five
          // job slots, which is the platform working on your behalf, and a page
          // with nothing moving anywhere in it reads as broken. It is chrome,
          // not the signature. `.live-dot` is already silenced under reduced
          // motion by `index.css`.
          className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${frame.dotClass} ${
            isAnimating(state) ? "live-dot" : ""
          }`}
        />
        <div>
          <div className="text-[0.86rem] font-semibold">{frame.headline}</div>
          <div className="text-[0.72rem] text-dim">
            {frame.heated
              ? HEAT_PHASE_TEXT[thermal]
              : "simulated annealing over a shift of trips"}
          </div>
        </div>
      </div>

      <motion.div
        aria-hidden="true"
        className="grid h-[132px] gap-[3px] rounded-lg border border-line bg-paper p-2"
        style={{
          gridTemplateColumns: `repeat(${COLUMNS}, minmax(0, 1fr))`,
          gridAutoRows: "1fr",
          // The swell reads as the field drawing breath rather than as the
          // panel resizing.
          transformOrigin: "bottom",
        }}
        initial={initialFrame}
        animate={inhaling ? INHALE_KEYFRAMES : AT_REST}
        transition={
          reducedMotion
            ? { duration: 0 }
            : inhaling
              ? { duration: DURATION.inhale, ease: EASE.decelerate }
              : { duration: DURATION.base, ease: EASE.standard }
        }
      >
        {lit.map((_, index) => (
          <span
            key={index}
            className={`rounded-[2px] border ${cellClassAt(index)} ${
              reducedMotion ? "" : "transition-colors"
            }`}
            style={
              reducedMotion
                ? undefined
                : {
                    transitionDuration: `${fadeMs}ms`,
                    transitionDelay: `${CELL_DELAY_MS[index]}ms`,
                  }
            }
          />
        ))}
      </motion.div>

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
