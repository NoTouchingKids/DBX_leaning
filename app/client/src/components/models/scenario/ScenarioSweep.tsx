/**
 * The `scenario` signature: a raster scan across the demand x capacity grid.
 *
 * Deliberately a scan and not a flicker. `gurobi_scheduling` searches — its
 * cells have no order and its animation says so. This model enumerates, in a
 * fixed order it cannot deviate from, and the one thing the animation exists
 * to communicate is that difference. Someone who has seen both should be able
 * to tell which is running from the far side of a desk.
 *
 * It is a single cursor and not several, which is worth stating because
 * "fan-out" is in this model's own docstring: `ScenarioModel.scenarios()` is
 * one `itertools.product`, evaluated in sequence. The fan-out is across RUNS,
 * against the five-concurrent-task ceiling — not inside one. Drawing parallel
 * heads would be inventing concurrency the model does not have.
 *
 * ## The phases of `motion.ts`, as frames
 *
 *   idle       the grid dimmed, no cursor. Nothing has been asked for.
 *   starting   the cursor arrives at the origin cell over DURATION.inhale —
 *              scaling down into place from oversize, fading up — and then
 *              HOLDS. A cold job sits here for tens of seconds, so the held
 *              frame has to differ from idle by more than a finished motion.
 *   running    the cursor breathes at DURATION.ambient, forever, and lands
 *              with a short scale gesture each time a real progress message
 *              moves it. The breath is what makes the panel legible between
 *              batches; the landing is what makes a jump legible as an event.
 *   settled    one flat frame over the whole grid, one gesture, no stagger.
 *              Nothing loops, drifts or pulses, and the legend goes with it.
 *
 * ## What the previous version got wrong
 *
 *  - **Nothing moved between batches.** Progress is emitted every ten
 *    scenarios or every second, and the cursor only moved when one arrived. A
 *    RUNNING run that had not yet reported drew no cursor at all, so a live
 *    sweep and a dead page were pixel-identical. That is the failure
 *    `motion.ts` names, and it is worse here than elsewhere: 72 scenarios over
 *    24 cells means most of what is on screen is the gap between jumps.
 *  - **STARTING parked a solid head on cell 0** in exactly the colour a real
 *    report uses. The honesty note admitted it in prose; the picture did not.
 *    The cursor now has two forms — a soft outline for "asked, nothing
 *    reported" and a solid fill for "this cell was reported" — so the
 *    distinction is visible rather than merely disclosed.
 *  - **The legend outlived the run.** Four cell states were still named under
 *    a grid that had gone to one flat colour, which is precisely the
 *    per-element meaning `contract.ts` says must not survive the end.
 *  - Timings were a hand-picked 300ms/35ms, predating `motion.ts`.
 *  - The evaluated trail was `index < head`, which excluded the reported cell
 *    itself — but `last_scenario` is a scenario that has already been
 *    evaluated, so it belongs in the trail.
 *
 * ## Why the cells are not `motion` components
 *
 * The 24 cells only ever do discrete colour swaps, which a CSS transition does
 * on the compositor for the cost of a style object, and this component
 * re-renders on every log line. Two things genuinely animate over time — the
 * grid's lift out of idle and the cursor — and those are the only two `motion`
 * elements in the file. The per-column transition delay turns each jump into a
 * short left-to-right cascade so a jump of three cells still reads as a sweep
 * rather than a blink; that cascade is the only decorative timing in here.
 */

import { motion } from "motion/react";
import { useState, type CSSProperties, type ReactNode } from "react";

import { isAnimating, isSettled, type ModelViewProps } from "@/components/models/contract";
import { DURATION, EASE, phaseOf, staggerFor, type MotionPhase } from "@/components/models/motion";
import { usePrefersReducedMotion } from "@/components/models/useReducedMotion";
import type { UiRunState } from "@/lib/envelope";
import { DOT_COLOR } from "@/components/ui/runStateStyles";

import { deriveSweep, SCAN_CAPACITY, SCAN_CELLS, SCAN_COLS, SCAN_DEMAND } from "./scenarioModel";

/** One flat frame per terminal state, applied to every cell. Nothing
 *  per-cell survives the end of a run — see the note in `contract.ts`. */
const TERMINAL_CELL = {
  SUCCEEDED: "bg-good-soft border-good",
  FAILED: "bg-bad-soft border-bad",
  CANCELLED: "bg-idle-soft border-idle",
  INFEASIBLE: "bg-warn-soft border-warn",
} as const;

type TerminalState = keyof typeof TERMINAL_CELL;

/** `isSettled` is the authority on whether a run is over; this only has to
 *  agree with it. Returning `undefined` for a live state is what keeps
 *  `cellTone` from having to know about the lifecycle twice. */
function terminalTone(state: UiRunState | null): string | undefined {
  return state !== null && state in TERMINAL_CELL
    ? TERMINAL_CELL[state as TerminalState]
    : undefined;
}

/** The grid before a run exists. Low enough to read as "not asked yet", high
 *  enough that the axes are still legible — someone should be able to see what
 *  is about to be swept before they sweep it. */
const REST_OPACITY = 0.45;

/** The cursor overhangs its cell slightly so it reads as something sitting ON
 *  the grid rather than as one more coloured cell, and drops in from larger
 *  still. Both are pure emphasis, so both go under reduced motion. */
const CURSOR_SCALE = 1.08;
const CURSOR_LAND_SCALE = 1.32;

/** The dim end of the ambient breath. Deep enough to be unmistakable across a
 *  room, shallow enough that a cell blinking every 2.4s for ten minutes is not
 *  the thing you cannot stop looking at. */
const CURSOR_DIM = 0.42;

/**
 * The cursor's three targets, and its two transitions, hoisted.
 *
 * Not tidiness: `motion` restarts a keyframe animation when the target it is
 * handed is not the one it is already running, and this component re-renders
 * on every log line of a chatty run. A fresh `[1, 0.42, 1]` literal per render
 * is a breath that keeps being cut off at frame one, which on a long sweep
 * looks like a stutter nobody can find the cause of.
 */
const CURSOR_AT_REST = { opacity: 1, scale: CURSOR_SCALE };
const CURSOR_BREATHING = { opacity: [1, CURSOR_DIM, 1], scale: CURSOR_SCALE };
/** Reduced motion: the cursor still says where the head is, flat and full. */
const CURSOR_FLAT = { opacity: 1, scale: 1 };

/** Arriving at STARTING is an appearance and fades up; landing during RUNNING
 *  is a MOVE, and starting that from zero opacity puts one blank frame in the
 *  middle of the gesture. Same drop, two entry points. */
const CURSOR_ARRIVE = { opacity: 0, scale: CURSOR_LAND_SCALE };
const CURSOR_LAND = { opacity: 1, scale: CURSOR_LAND_SCALE };

const BREATH = {
  duration: DURATION.ambient,
  times: [0, 0.5, 1],
  ease: EASE.standard,
  repeat: Infinity,
};
const FADE_IN = { duration: DURATION.inhale, ease: EASE.decelerate };

const CAPTION: Record<string, [string, string]> = {
  none: ["No run selected", "Trigger a sweep to watch it scan"],
  QUEUED: ["Queued", "Waiting for compute"],
  STARTING: ["Starting sweep", "Preparing the scenario grid"],
  RUNNING: ["Sweeping scenarios", "Evaluating grid cells in order"],
  SUCCEEDED: ["Sweep complete", "The whole grid was enumerated"],
  FAILED: ["Sweep failed", "The run did not complete"],
  CANCELLED: ["Sweep cancelled", "Stopped part-way through the grid"],
  INFEASIBLE: ["Sweep reported infeasible", "No scenario produced a usable outcome"],
};

function captionFor(state: UiRunState | null): [string, string] {
  return CAPTION[state ?? "none"] ?? CAPTION["none"] ?? ["", ""];
}

export function ScenarioSweep({ state, snapshot }: ModelViewProps) {
  const reduced = usePrefersReducedMotion();
  const phase = phaseOf(state);
  const sweep = deriveSweep(snapshot.progress);
  const settled = isSettled(state);
  const terminal = settled ? terminalTone(state) : undefined;

  const moving = phase === "starting" || phase === "running";

  // Two separate things, deliberately not one. `trail` is drawn only from a
  // cell the run actually REPORTED; `cursor` also exists while the run has
  // reported nothing, parked on the origin — which is a spin-up frame, not a
  // claim, and is drawn in its own outline form to say so.
  const trail = moving ? sweep.head : null;
  const cursor = moving ? (sweep.head ?? 0) : null;
  const pending = sweep.head === null;

  const best = settled ? null : sweep.bestCell;
  // Usually true: progress batches ten scenarios, so an improvement lands on
  // the batch's final member about one time in ten. The dashed form is the
  // honest picture of "somewhere around here".
  const bestApprox = best !== null && !sweep.bestCellExact;

  /** A run exists at all. Deliberately not `phase !== "idle"`: `phaseOf` calls
   *  QUEUED idle, correctly, because nothing may MOVE there — but dimming the
   *  grid back down between STARTING and RUNNING would read as the run being
   *  un-asked for a second. The lift is a fact about whether a run exists; the
   *  phase decides what, if anything, animates. */
  const awake = state !== null;

  // Axis emphasis is a coordinate readout, so it is drawn only when the head is
  // a matched coordinate. On a custom grid `locateCell` falls back to a
  // proportional placement — real progress, but naming a demand and a capacity
  // off the back of it would be a fabrication.
  const named = moving && sweep.head !== null && sweep.headExact ? sweep.head : null;
  const axisRow = named === null ? null : Math.floor(named / SCAN_COLS);
  const axisCol = named === null ? null : named % SCAN_COLS;

  const [line1, defaultLine2] = captionFor(state);
  const line2 =
    settled || state !== "RUNNING"
      ? defaultLine2
      : sweep.head === null
        ? "Started; no scenario reported yet"
        : sweep.headExact
          ? defaultLine2
          : "Custom grid — the cursor sits at the completed fraction, not a coordinate";

  const label = settled
    ? `Scenario sweep ${state}`
    : sweep.head === null
      ? "Scenario sweep, no scenarios reported yet"
      : `Scenario sweep, evaluating cell ${sweep.head + 1} of ${SCAN_CELLS}` +
        (sweep.scenariosTotal !== null
          ? ` (${sweep.scenariosDone ?? 0} of ${sweep.scenariosTotal} scenarios)`
          : "");

  // One transition per COLUMN, not per cell: the cascade runs along the demand
  // axis, so every cell in a column shares a delay and there are six of these.
  // The terminal frame takes no stagger at all — a flat state applied to the
  // whole grid is one gesture, and rippling it would imply the cells arrived
  // at it separately.
  const step = reduced || settled ? 0 : staggerFor(SCAN_COLS);
  const seconds = settled ? DURATION.slow : DURATION.base;
  const columnTransition: (CSSProperties | undefined)[] = Array.from(
    { length: SCAN_COLS },
    (_, col) =>
      reduced
        ? // Not the `motion-reduce:` variant: an inline `transitionProperty`
          // outranks a class, so the class would silently lose this argument.
          { transitionProperty: "none" }
        : {
            transitionProperty: "background-color, border-color",
            transitionDuration: `${seconds}s`,
            transitionTimingFunction: `cubic-bezier(${EASE.standard.join(", ")})`,
            transitionDelay: `${(col * step).toFixed(3)}s`,
          },
  );

  // Evaluated once. Mounting straight into STARTING — the common case, since
  // triggering a run navigates here — should still get the lift; mounting into
  // any other phase must not animate in, because a finished run that fades up
  // on arrival is motion after the end of the run.
  const [enterFrom] = useState<{ opacity: number } | false>(() =>
    phase === "starting" && !reduced ? { opacity: REST_OPACITY } : false,
  );

  return (
    <div>
      <div className="mb-3 flex items-center gap-2.5">
        <span
          // Pulses through QUEUED and STARTING too, where the grid itself is
          // holding still and has nothing to say. A queued run is waiting on
          // one of the five job slots — the platform working on your behalf —
          // and a panel with nothing moving anywhere in it reads as broken.
          // This is chrome, not the signature; `index.css` already silences it
          // under reduced motion.
          className={
            `inline-block h-2 w-2 shrink-0 rounded-full bg-current ${DOT_COLOR[state ?? "QUEUED"]} ` +
            (isAnimating(state) ? "live-dot" : "")
          }
        />
        <div>
          <div className="text-[0.82rem] font-semibold">{line1}</div>
          <div className="text-[0.72rem] text-dim">{line2}</div>
        </div>
      </div>

      <div className="overflow-x-auto">
        <motion.div
          role="img"
          aria-label={label}
          className="inline-grid gap-[3px]"
          style={{ gridTemplateColumns: `2.9rem repeat(${SCAN_COLS}, minmax(2rem, 1fr))` }}
          initial={enterFrom}
          animate={{ opacity: awake ? 1 : REST_OPACITY }}
          transition={
            reduced
              ? { duration: 0 }
              : { duration: phase === "starting" ? DURATION.inhale : DURATION.base, ease: EASE.decelerate }
          }
        >
          <div />
          {SCAN_DEMAND.map((value, col) => (
            <AxisLabel key={`d${value}`} on={col === axisCol} className="text-center">
              d{value}
            </AxisLabel>
          ))}

          {SCAN_CAPACITY.map((capacity, row) => (
            <Row
              key={`c${capacity}`}
              capacity={capacity}
              row={row}
              axisOn={row === axisRow}
              trail={trail}
              cursor={cursor}
              pending={pending}
              best={best}
              bestApprox={bestApprox}
              terminal={terminal}
              phase={phase}
              reduced={reduced}
              columnTransition={columnTransition}
            />
          ))}
        </motion.div>
      </div>

      {settled ? (
        // The legend is retired with the run. Four named cell states under a
        // grid that has gone to one colour is exactly the per-element meaning
        // `contract.ts` says must not survive the end.
        <p className="mt-3 text-[0.68rem] text-dim">
          One flat frame — past the end of a run no cell means anything different from any other.
          The numbers are in the two cards below.
        </p>
      ) : (
        // Load-bearing while the run is live: four discrete cell states with no
        // magnitude anywhere means the colours have to be named. Two of the
        // four keys switch with the picture rather than describing a fixed
        // encoding, because both of those marks mean two different things and a
        // key that named only one of them would be the wrong one half the time.
        <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[0.68rem] text-dim">
          <Key className="border-edge bg-paper" text="not yet reached" />
          <Key className="border-info bg-info-soft" text="evaluated" />
          {pending ? (
            <Key className="border-accent bg-accent-soft" text="sweep starts here — nothing reported yet" />
          ) : (
            <Key className="border-info bg-info" text="last cell the run reported" />
          )}
          {bestApprox ? (
            <Key
              className="border-dashed border-accent bg-accent-soft"
              text="best objective, somewhere in this batch"
            />
          ) : (
            <Key className="border-accent bg-accent" text="best objective so far" />
          )}
        </div>
      )}
    </div>
  );
}

function Row({
  capacity,
  row,
  axisOn,
  trail,
  cursor,
  pending,
  best,
  bestApprox,
  terminal,
  phase,
  reduced,
  columnTransition,
}: {
  capacity: number;
  row: number;
  axisOn: boolean;
  trail: number | null;
  cursor: number | null;
  pending: boolean;
  best: number | null;
  bestApprox: boolean;
  terminal: string | undefined;
  phase: MotionPhase;
  reduced: boolean;
  columnTransition: readonly (CSSProperties | undefined)[];
}) {
  return (
    <>
      <AxisLabel on={axisOn} className="flex items-center justify-end pr-1">
        c{capacity}
      </AxisLabel>
      {SCAN_DEMAND.map((demand, col) => {
        const index = row * SCAN_COLS + col;
        return (
          <div
            key={`${capacity}-${demand}`}
            title={`demand ${demand} x capacity ${capacity}`}
            className={
              "relative h-[1.35rem] rounded-[3px] border " +
              cellTone(index, trail, best, bestApprox, terminal)
            }
            style={columnTransition[col]}
          >
            {/* No `key`: the cursor lives inside whichever cell it is on, so
                moving it is already a mount in a new parent, which is what
                restarts the landing gesture on every real jump. */}
            {index === cursor && <ScanCursor pending={pending} phase={phase} reduced={reduced} />}
          </div>
        );
      })}
    </>
  );
}

/**
 * The scan head, drawn OVER the grid rather than as one more cell colour.
 *
 * An overlay because the cell underneath keeps its own CSS colour transition —
 * a cursor that was the cell's background would have to fight that transition
 * for the same property, and the cascade behind it would stutter every jump.
 *
 * Two forms, one element. `pending` is "a run exists and has reported nothing
 * yet", which covers the whole STARTING frame and the head of a RUNNING one:
 * a soft outline, sitting on the origin, claiming only that the sweep starts
 * there. The solid fill is reserved for a cell the run actually named.
 *
 * It leaves by being unmounted, with no exit animation — a hard cut against
 * the grid's 0.55s wash into the terminal colour. That is a choice, not an
 * oversight: the cursor is lifted and the grid resolves, which is a decisive
 * enough gesture on its own, and `AnimatePresence` around one element whose
 * exit ends in a frozen frame is more machinery than the moment is worth.
 */
function ScanCursor({
  pending,
  phase,
  reduced,
}: {
  pending: boolean;
  phase: MotionPhase;
  reduced: boolean;
}) {
  // The ambient loop belongs to RUNNING alone. STARTING gets one inhale and
  // then holds; looping there would spend the phase's whole vocabulary on it.
  const breathing = phase === "running" && !reduced;

  return (
    <motion.span
      aria-hidden="true"
      className={
        "absolute inset-0 rounded-[3px] border " +
        (pending ? "border-accent bg-accent-soft" : "border-info bg-info")
      }
      initial={reduced ? false : phase === "starting" ? CURSOR_ARRIVE : CURSOR_LAND}
      animate={reduced ? CURSOR_FLAT : breathing ? CURSOR_BREATHING : CURSOR_AT_REST}
      transition={
        reduced
          ? { duration: 0 }
          : {
              opacity: breathing ? BREATH : FADE_IN,
              // Carries both the arrival at STARTING, stretched over the whole
              // inhale, and the much shorter landing on each RUNNING jump. The
              // breath's keyframes start at 1, so on a jump the scale is the
              // entire gesture and it has to be quick enough to read as one.
              scale: {
                duration: phase === "starting" ? DURATION.inhale : DURATION.base,
                ease: EASE.decelerate,
              },
            }
      }
    />
  );
}

function cellTone(
  index: number,
  trail: number | null,
  best: number | null,
  bestApprox: boolean,
  terminal: string | undefined,
): string {
  if (terminal !== undefined) return terminal;
  // An improvement found ON the reported cell puts the amber under the cursor,
  // where it stays hidden until the next batch moves the cursor off it. Left
  // that way on purpose: covering it needs a third cursor form and a third
  // legend key, to buy one batch of visibility for something the header and the
  // "currently evaluating" card are both already reporting as a number.
  if (best !== null && index === best) {
    return bestApprox ? "bg-accent-soft border-dashed border-accent" : "bg-accent border-accent";
  }
  // `<=`, not `<`: `last_scenario` is a scenario the run has FINISHED, so the
  // reported cell belongs to the evaluated trail. The cursor sits on top of it.
  if (trail !== null && index <= trail) return "bg-info-soft border-info";
  return "bg-paper border-edge";
}

/**
 * Row and column labels. `on` names the head's own coordinate — the only place
 * the grid says which scenario is being evaluated without anyone reading a
 * number, and drawn only when that coordinate was matched rather than
 * interpolated.
 *
 * No transition on it, deliberately. This is information arriving, not motion,
 * and it moves in step with the cursor's jump; easing it would put the label
 * and the cell it describes out of phase. Semibold costs no width in a mono
 * face, so the emphasis cannot shift the grid's columns either.
 */
function AxisLabel({
  on,
  className,
  children,
}: {
  on: boolean;
  className: string;
  children: ReactNode;
}) {
  return (
    <div
      className={
        `${className} font-mono text-[0.6rem] ` + (on ? "font-semibold text-ink" : "text-faint")
      }
    >
      {children}
    </div>
  );
}

function Key({ className, text }: { className: string; text: string }) {
  return (
    <span className="flex items-center gap-1.5">
      <span className={`inline-block h-3 w-5 rounded-[3px] border ${className}`} />
      {text}
    </span>
  );
}
