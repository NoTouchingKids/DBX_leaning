/**
 * The `streaming_results` signature: a window sliding across the series.
 *
 * `contract.ts` allows a signature to track something real "where a natural
 * hook exists", and this model has an unusually direct one: the window's
 * position is derived entirely from `windows_done`, so it moves when, and only
 * when, a real window finished and its chunk of results went out. That has not
 * changed and is the point of the view — nothing below puts the window on a
 * clock.
 *
 * ## The lifecycle phases, per `motion.ts`
 *
 *   idle      an empty track: twelve low rails, no window, nothing moving.
 *             QUEUED lands here because `phaseOf` says so — a run waiting on
 *             one of the five job slots has no origin to report yet.
 *   starting  the window unrolls from the left edge once, over
 *             DURATION.inhale, and then HOLDS. It does not loop back, and it
 *             stays parked there until a real placement arrives.
 *   running   segments rise and colour as chunks land — real — and one pale
 *             sweep crosses the window per DURATION.ambient, which is pacing.
 *   settled   the track flattens to one colour at full height, the window
 *             fades out, and nothing moves again.
 *
 * The middle two are the reason this was reworked rather than tidied.
 *
 * There was no starting frame at all: the window was parked at segment 0 with
 * no motion, and a cold Databricks job sits in that state for tens of seconds.
 * And there was nothing ambient anywhere, which is defensible right up until
 * you watch a real run — the panel then looks disconnected rather than
 * patient, and a viewer cannot tell a live run from a stalled one without
 * reading the log pane.
 *
 * The sweep is the answer to that, and it is deliberately confined INSIDE the
 * window frame. A pacing loop on the track itself would be indistinguishable
 * from progress along the track, which is the one thing here that is real; a
 * loop inside the window can only ever say "this window is being fit", which
 * is true for as long as the window is on screen.
 *
 * ## Why the window is drawn in two parts
 *
 * The old frame was a plain box, and a plain box sliding along a filling bar
 * is a progress bar with an ornament on it — nothing about it says *backtest*.
 * The frame is now a fit region and, past a dashed divider, the horizon it
 * forecasts into. That split is what makes the model recognisable at a glance,
 * and it is drawn rather than measured: the progress payload carries
 * `windows_done`, `windows_total` and `origin`, and neither the window size
 * nor the horizon is on the wire for this view to read.
 *
 * ## What is real (mirrored in the view's `honesty` note)
 *
 * Real: where the window sits, how many segments stand at full height, and the
 * counts in the readout — all of them `placeWindow` off `windows_done`.
 *
 * Decorative: the frame's footprint and its fit/horizon split, and the sweep.
 */

import { AnimatePresence, motion } from "motion/react";

import { isAnimating, isSettled, type ModelViewProps } from "@/components/models/contract";
import { DURATION, EASE, SPRING, phaseOf, staggerFor } from "@/components/models/motion";
import { usePrefersReducedMotion } from "@/components/models/useReducedMotion";
import type { UiRunState } from "@/lib/envelope";
import { DOT_COLOR } from "@/components/ui/runStateStyles";
import { EMPTY, formatCount } from "@/lib/format";

import { placeWindow, TIMELINE_SEGMENTS, WINDOW_SEGMENTS } from "./streamingModel";

/** One flat fill for every segment once the run is over. Solid, not the soft
 *  tints these used to be: a segment at rest is now a thin rail rather than a
 *  full-height box, and a tint that pale on an 11px rail is invisible. */
const TERMINAL_FILL: Partial<Record<UiRunState, string>> = {
  SUCCEEDED: "bg-good",
  FAILED: "bg-bad",
  CANCELLED: "bg-idle",
  INFEASIBLE: "bg-warn",
};

/** How tall a segment stands before its window has been backtested. Low
 *  enough that the completed run of segments reads as a filled bar from across
 *  a room, high enough that the empty track still reads as a track. */
const PENDING_SCALE = 0.34;

/** Seconds between neighbouring segments in the one place a group of them
 *  moves together: the settle, where every segment ahead of the fill front
 *  rises to full height at once. `staggerFor` rather than a flat step so the
 *  whole sweep is over inside half a second — a settle that is still finishing
 *  is a run that still looks like it is going. */
const SEGMENT_STAGGER = staggerFor(TIMELINE_SEGMENTS);

/** The sweep bar, as a fraction of the window frame. */
const SCAN_WIDTH = 0.25;
/** One frame width expressed in the bar's own widths: motion's percentage
 *  translate is relative to the element, not to its parent, so travelling the
 *  full frame is `100 / SCAN_WIDTH` percent and not 100. */
const SCAN_TRAVEL = 100 / SCAN_WIDTH;

const CAPTION: Record<string, [string, string]> = {
  none: ["No run selected", "Trigger a backtest to watch it roll"],
  QUEUED: ["Queued", "Waiting for compute"],
  STARTING: ["Starting backtest", "Preparing the first window"],
  RUNNING: ["Backtesting windows", "Rolling the origin forward"],
  SUCCEEDED: ["Backtest complete", "Every window forecast and scored"],
  FAILED: ["Backtest failed", "The run did not complete"],
  CANCELLED: ["Backtest cancelled", "Stopped between windows; earlier chunks stand"],
  INFEASIBLE: ["Backtest reported infeasible", "No window could be scored"],
};

function captionFor(state: UiRunState | null): [string, string] {
  return CAPTION[state ?? "none"] ?? CAPTION["none"] ?? ["", ""];
}

export function RollingWindow({ state, snapshot }: ModelViewProps) {
  const reduced = usePrefersReducedMotion();
  const settled = isSettled(state);
  const phase = phaseOf(state);
  const place = placeWindow(snapshot.latestProgress);
  const [line1, line2] = captionFor(state);

  const terminalFill = settled ? TERMINAL_FILL[state ?? "SUCCEEDED"] : undefined;
  // Anything that is not idle and not over parks the window at the left until
  // a real placement lands: a spin-up frame, not a report.
  //
  // This used to be STARTING only, which left a hole. The status stream says
  // RUNNING well before the first window finishes — that gap is the whole cold
  // start — so the window vanished on the STARTING→RUNNING edge and came back
  // on the first progress message. With an exit animation on it that reads as
  // the run losing its place and finding it again.
  const start = settled || phase === "idle" ? null : (place.start ?? 0);
  const span = Math.min(WINDOW_SEGMENTS, TIMELINE_SEGMENTS - (start ?? 0));
  // The last segment of the footprint is the horizon; everything left of it is
  // the fit region. `max(1, …)` only guards a future WINDOW_SEGMENTS of 1.
  const fitShare = Math.max(1, span - 1) / span;

  // Delays are measured FORWARD FROM THE FILL FRONT, not from the left edge of
  // the track. A blind per-index delay would put a fixed 0.4s lag on segment 11
  // every time it filled, which is the animation describing the track instead
  // of the run. Everything at or behind the front gets zero, so the one segment
  // that lands mid-run lands immediately; the ramp is only ever spent on the
  // segments ahead of the front, which is exactly the set that rises together
  // when the run settles.
  const delayFor = (index: number): number =>
    reduced ? 0 : Math.max(0, index - place.segmentsDone) * SEGMENT_STAGGER;

  // The settle is one gesture, so it gets the longer curve; a single segment
  // landing mid-run is a state swap and gets the ordinary one.
  const washSeconds = reduced ? 0 : settled ? DURATION.slow : DURATION.base;

  const inhaling = phase === "starting" && !reduced;

  const label = settled
    ? `Rolling backtest ${state}`
    : place.windowsDone === null
      ? "Rolling backtest, no windows reported yet"
      : `Rolling backtest, ${place.windowsDone} of ${place.windowsTotal ?? "?"} windows complete`;

  return (
    // No padding and no card chrome: the page wraps this in a Card that
    // already supplies both, and drawing either here doubles it up.
    <div>
      <div className="mb-3 flex items-center gap-2.5">
        <span
          className={
            `inline-block h-2 w-2 shrink-0 rounded-full bg-current ${DOT_COLOR[state ?? "QUEUED"]} ` +
            // Chrome, not part of the signature — hence QUEUED and STARTING
            // too, where the track itself has nothing to say and a page with
            // nothing moving anywhere in it reads as broken. `index.css`
            // already silences `.live-dot` under reduced motion.
            (isAnimating(state) ? "live-dot" : "")
          }
        />
        <div>
          <div className="text-[0.82rem] font-semibold">{line1}</div>
          <div className="text-[0.72rem] text-dim">{line2}</div>
        </div>
      </div>

      <div role="img" aria-label={label} className="relative flex h-[2.1rem] gap-[3px] px-[2px]">
        {Array.from({ length: TIMELINE_SEGMENTS }, (_, index) => {
          const done = index < place.segmentsDone;
          return (
            <motion.div
              key={index}
              className={
                "flex-1 rounded-[3px] transition-colors " +
                (terminalFill ?? (done ? "bg-info" : "bg-edge"))
              }
              style={{
                transitionDuration: `${washSeconds}s`,
                transitionDelay: `${delayFor(index)}s`,
              }}
              // Never a mount entrance. A run opened from history arrives
              // already finished, and washing its track in would animate
              // windows that were backtested before the page existed.
              initial={false}
              // Settled flattens every segment: per `contract.ts` no
              // per-element meaning survives the end of a run, so a failed run
              // at window 3 of 33 does not keep three tall segments. The count
              // it stopped at is still in the readout below.
              animate={{ scaleY: settled || done ? 1 : PENDING_SCALE }}
              transition={
                reduced ? { duration: 0 } : { ...SPRING.snappy, delay: delayFor(index) }
              }
            />
          );
        })}

        <AnimatePresence>
          {start !== null && (
            <motion.div
              key="window"
              className="pointer-events-none absolute -top-1 -bottom-1 overflow-hidden rounded-[5px] border-2 border-accent"
              // Left, not centre: the inhale is the window unrolling forward
              // from the origin it starts at, not growing out of its own middle.
              style={{ transformOrigin: "left center" }}
              initial={inhaling ? { scaleX: 0, opacity: 0 } : false}
              animate={{
                left: `${(start / TIMELINE_SEGMENTS) * 100}%`,
                width: `${(span / TIMELINE_SEGMENTS) * 100}%`,
                scaleX: 1,
                opacity: 1,
              }}
              // Leaving accelerates away, per `motion.ts`. This runs once, as
              // part of the settling gesture — the window has no position left
              // to report, so it goes rather than freezing somewhere.
              exit={{
                opacity: 0,
                transition: { duration: reduced ? 0 : DURATION.base, ease: EASE.accelerate },
              }}
              transition={
                reduced
                  ? { duration: 0 }
                  : {
                      // Per-value, because the two are different events: the
                      // step is a report landing, the inhale is the frame
                      // saying "asked, not yet answered".
                      default: { duration: DURATION.slow, ease: EASE.standard },
                      scaleX: { duration: DURATION.inhale, ease: EASE.decelerate },
                      opacity: { duration: DURATION.inhale, ease: EASE.decelerate },
                    }
              }
            >
              {/* Fit region and, past the divider, the horizon it forecasts
                  into. The horizon is drawn as the emptiness that is left
                  rather than as a second box — one dashed line is the whole
                  cost of saying which half is which. */}
              <div
                className="absolute inset-y-0 left-0 border-r border-dashed border-accent bg-accent/12"
                style={{ width: `${fitShare * 100}%` }}
              />

              {phase === "running" && !reduced && (
                /* Ambient. Seam-free the way the forecasting sweep is: it fades
                   up after it has entered and down before it leaves, so the wrap
                   back to the left edge is never visible. Linear on purpose — an
                   eased traverse spends its slow ends where the bar is already
                   invisible and its fast middle where it is not, which reads as
                   a dart rather than a drift. */
                <motion.div
                  className="absolute inset-y-0 left-0 bg-accent"
                  style={{ width: `${SCAN_WIDTH * 100}%` }}
                  animate={{
                    x: ["-100%", "-40%", `${SCAN_TRAVEL - 60}%`, `${SCAN_TRAVEL}%`],
                    // Low enough to sit under the fit tint without competing
                    // with the filled segments showing through it.
                    opacity: [0, 0.32, 0.32, 0],
                  }}
                  transition={{
                    duration: DURATION.ambient,
                    times: [0, 0.12, 0.72, 1],
                    repeat: Infinity,
                    ease: "linear",
                  }}
                />
              )}
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      <div className="mt-3 flex flex-wrap justify-between gap-x-5 gap-y-1 font-mono text-[0.7rem] text-dim">
        <span>
          windows{" "}
          <span className="font-semibold text-ink">
            {place.windowsDone === null ? EMPTY : formatCount(place.windowsDone)}
            {place.windowsTotal === null ? "" : ` / ${formatCount(place.windowsTotal)}`}
          </span>
        </span>
        <span>
          origin{" "}
          <span className="font-semibold text-ink">
            {place.origin === null ? EMPTY : formatCount(place.origin)}
          </span>
        </span>
        <span className="text-faint">
          {place.windowsTotal === null
            ? `${TIMELINE_SEGMENTS}-segment track`
            : place.lockstep
              ? "one step per window"
              : `${formatCount(place.windowsTotal)} windows across ${TIMELINE_SEGMENTS} segments`}
        </span>
      </div>
    </div>
  );
}
