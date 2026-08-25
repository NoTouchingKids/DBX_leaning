/**
 * The lattice as a state machine over the run lifecycle.
 *
 * Kept out of the component for the reason `contract.ts` gives: a terminal run
 * gets ONE flat frame for the whole lattice — every cell the same, motion
 * stopped — so a finished run reads as finished without anyone parsing it.
 *
 * The consequence worth stating, because it looks like a contradiction: the
 * fitted/failed split is the headline of this view, and yet it STOPS being
 * drawn in the lattice the moment the run ends. That is deliberate. The
 * lattice is the animation, and the contract does not let per-element meaning
 * survive the end of a run. The split does not disappear — it lives in the
 * outcome bar and the readout under the lattice, which are data rather than
 * animation and are therefore identical in every state, terminal included.
 * `PanelLattice` renders both, and the honesty note says which is which.
 *
 * INFEASIBLE is a DESIGNED state here, not defensive cover the way it is in
 * `annealing/frames.ts`. `models/panel_fit/model.py::_terminal_status` returns
 * it explicitly when `groups_fitted == 0`: the run completed, the results are
 * correct and durable, and the answer is that nothing could be fitted. It must
 * not read as a crash.
 */

import type { UiRunState } from "@/lib/envelope";

export interface LatticeFrame {
  /** Tailwind classes for every cell in a flat (terminal) frame. */
  flatCellClass: string;
  dotClass: string;
  headline: string;
  detail: string;
  /** Whether cells are drawn per-outcome. False in every terminal state. */
  split: boolean;
  /** Whether the frontier cell pulses. */
  animated: boolean;
}

export function frameFor(state: UiRunState | null): LatticeFrame {
  switch (state) {
    case null:
      return {
        flatCellClass: "border-line bg-paper opacity-50",
        dotClass: "bg-idle",
        headline: "No run selected",
        detail: "Trigger a fit to watch the panel resolve group by group",
        split: false,
        animated: false,
      };
    case "QUEUED":
      return {
        flatCellClass: "border-line bg-paper",
        dotClass: "bg-info",
        headline: "Queued",
        detail: "Waiting for one of the five account-wide job slots",
        split: false,
        animated: false,
      };
    case "STARTING":
      return {
        flatCellClass: "border-accent/40 bg-accent-soft",
        dotClass: "bg-accent",
        headline: "Starting",
        detail: "The job is spinning up; the panel is loaded before the first fit",
        split: false,
        animated: false,
      };
    case "RUNNING":
      return {
        // Unused while `split` is true; the frame a running run falls back to
        // before any group has been reported.
        flatCellClass: "border-line bg-paper",
        dotClass: "bg-info",
        headline: "Fitting groups",
        detail: "One least-squares fit per group, each independent of the others",
        split: true,
        animated: true,
      };
    case "SUCCEEDED":
      return {
        flatCellClass: "border-good bg-good-soft",
        dotClass: "bg-good",
        headline: "Every group processed",
        // Said plainly because it is the whole point of the model: a run can
        // succeed with units failed, and that is not a contradiction.
        detail: "Groups that could not be fitted are recorded with a reason, not dropped",
        split: false,
        animated: false,
      };
    case "FAILED":
      return {
        flatCellClass: "border-bad bg-bad-soft",
        dotClass: "bg-bad",
        headline: "Run failed",
        detail: "The run itself stopped — distinct from a group failing to fit",
        split: false,
        animated: false,
      };
    case "CANCELLED":
      return {
        flatCellClass: "border-idle bg-idle-soft opacity-70",
        dotClass: "bg-idle",
        headline: "Cancelled",
        // True of this model specifically: the cancel check fires between
        // groups, never mid-fit, and the trailing flush still runs.
        detail: "Stopped between groups; every group already processed was written",
        split: false,
        animated: false,
      };
    case "INFEASIBLE":
      return {
        flatCellClass: "border-warn bg-warn-soft",
        dotClass: "bg-warn",
        headline: "No group could be fitted",
        detail:
          "The model's own verdict, not a crash: it ran to completion and every group failed, so it reported INFEASIBLE rather than a success with nothing in it",
        split: false,
        animated: false,
      };
  }
}

/* ================================================================== *
 * Tone, as pixels
 * ================================================================== */

/**
 * Classes per `FailureTone`. `warn` is as far as this goes — there is no
 * `bad` branch, and `FailureTone` has no member that could reach one.
 */
export const TONE_CLASS = {
  none: { text: "text-dim", chip: "border-line text-faint", bar: "bg-idle" },
  routine: { text: "text-info", chip: "border-info text-info", bar: "bg-info" },
  notable: { text: "text-warn", chip: "border-warn text-warn", bar: "bg-warn" },
} as const;

export const TONE_NOTE = {
  none: "no group has failed to fit",
  routine: "some groups could not be fitted — normal for a panel with short units",
  notable: "at least half the completed groups could not be fitted — worth a look at the panel and the degree",
} as const;
