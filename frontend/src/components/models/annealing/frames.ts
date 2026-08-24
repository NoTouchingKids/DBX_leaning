/**
 * The signature animation as a state machine over the run lifecycle.
 *
 * Kept out of the component for the reason `contract.ts` gives: the frame is
 * the part that has to be right, and the part someone will be tempted to make
 * per-element later. A terminal run gets ONE flat frame for the whole lattice
 * — every cell the same, motion stopped — so a finished run reads as finished
 * without anyone parsing it.
 */

import type { UiRunState } from "@/lib/envelope";

export interface SignatureFrame {
  /** Tailwind classes for every cell in a flat (non-running) frame. */
  cellClass: string;
  /** The dot beside the caption. */
  dotClass: string;
  headline: string;
  /** Whether the lattice churns. False in every terminal state, and in
   *  QUEUED — nothing is searching yet. */
  animated: boolean;
  /** Whether cells take their colour from the live heat rather than from
   *  `cellClass`. Only true while the search is actually running. */
  heated: boolean;
}

/**
 * Six designed states — QUEUED, STARTING, RUNNING, SUCCEEDED, FAILED,
 * CANCELLED — plus the no-run-selected frame.
 *
 * INFEASIBLE is here as defensive cover, not as a seventh designed state:
 * this model prices overweight into the objective instead of constraining it,
 * so no annealing run can reach a status that means "no feasible solution
 * exists". If one ever does, it gets a distinct flat frame rather than being
 * quietly folded into FAILED.
 */
export function frameFor(state: UiRunState | null): SignatureFrame {
  switch (state) {
    case null:
      return {
        cellClass: "border-line bg-paper opacity-50",
        dotClass: "bg-idle",
        headline: "No run selected",
        animated: false,
        heated: false,
      };
    case "QUEUED":
      return {
        cellClass: "border-line bg-paper",
        dotClass: "bg-info",
        headline: "Queued — waiting for one of the five job slots",
        animated: false,
        heated: false,
      };
    case "STARTING":
      return {
        cellClass: "border-accent/40 bg-accent-soft",
        dotClass: "bg-accent",
        headline: "Starting — the job is spinning up",
        animated: false,
        heated: false,
      };
    case "RUNNING":
      return {
        // Unused while `heated` is true; kept as the frame a running run
        // falls back to before any temperature has been reported.
        cellClass: "border-accent/40 bg-accent-soft",
        dotClass: "bg-info",
        headline: "Annealing",
        animated: true,
        heated: true,
      };
    case "SUCCEEDED":
      return {
        cellClass: "border-good bg-good-soft",
        dotClass: "bg-good",
        headline: "Annealed — the best feasible shift was kept",
        animated: false,
        heated: false,
      };
    case "FAILED":
      return {
        cellClass: "border-bad bg-bad-soft",
        dotClass: "bg-bad",
        headline: "Failed",
        animated: false,
        heated: false,
      };
    case "CANCELLED":
      return {
        cellClass: "border-idle bg-idle-soft opacity-70",
        dotClass: "bg-idle",
        // True of this model specifically: `results()` returns the incumbent
        // whether or not the loop ran to the end, and the cancellation check
        // only fires on a `progress_every` boundary, so a cancel lands late
        // rather than instantly.
        headline: "Cancelled — the best shift found before the stop was kept",
        animated: false,
        heated: false,
      };
    case "INFEASIBLE":
      return {
        cellClass: "border-warn bg-warn-soft",
        dotClass: "bg-warn",
        headline: "Infeasible",
        animated: false,
        heated: false,
      };
  }
}

/* ================================================================== *
 * Heat, as pixels
 * ================================================================== */

/**
 * How the lattice moves at a given heat.
 *
 * Both numbers are the animation's whole claim to being paced by something
 * real, so they are derived rather than picked per state: at the start of a
 * default run cells change several times a second, and by the end a change is
 * a rare single event. That is the cooling schedule, felt rather than read.
 */
export interface HeatMotion {
  /** Milliseconds between churns. */
  intervalMs: number;
  /** Cells flipped per churn. */
  flipsPerTick: number;
  /** Colour-transition duration, in ms. Long at low heat so the few changes
   *  that do happen drift rather than snap. */
  transitionMs: number;
}

const HOT_INTERVAL_MS = 110;
const COLD_INTERVAL_MS = 1_400;

export function heatMotion(heat: number | null): HeatMotion {
  // Null heat means "running, no temperature yet", and annealing starts hot.
  const h = heat ?? 1;
  return {
    intervalMs: Math.round(COLD_INTERVAL_MS + (HOT_INTERVAL_MS - COLD_INTERVAL_MS) * h),
    flipsPerTick: Math.max(1, Math.round(1 + 9 * h)),
    transitionMs: Math.round(160 + 700 * (1 - h)),
  };
}

/**
 * Cell colour at a given heat: accent (warm) through info (cool).
 *
 * Three steps rather than a continuous mix, because a `color-mix` between two
 * tokens is not a token and would need re-checking against both palettes.
 * Three named token pairs are legible in light and dark by construction.
 */
export function heatCellClass(heat: number | null): string {
  const h = heat ?? 1;
  if (h >= 0.62) return "border-accent bg-accent-soft";
  if (h >= 0.22) return "border-accent/50 bg-accent-soft/60";
  return "border-info bg-info-soft";
}

/** The unlit cells. Constant across heats: only the lit set carries the
 *  temperature, so a changing background would double-count it. */
export const RESTING_CELL_CLASS = "border-line bg-paper";
