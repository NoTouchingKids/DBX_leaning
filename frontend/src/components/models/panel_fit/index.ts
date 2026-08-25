/**
 * `panel_fit` — the per-unit-outcome case.
 *
 * The only model on the platform that fits N units independently and lets
 * individual units FAIL while the run SUCCEEDS. Everything else here is one
 * computation with one verdict, so the generic view — `percent_complete` plus
 * a metric — draws a healthy run and a run quietly failing a third of its
 * groups identically. Making that impossible is the reason the model was
 * built, so the fitted/failed split is the headline of the signature rather
 * than a detail tucked into a corner.
 *
 * Two charts, and the pairing is the point: the first is the quality of the
 * fits that worked, the second is why the rest did not. A failed group has no
 * R-squared, so it is *structurally* absent from the first chart — the second
 * is not an appendix to it, it is the other half of the same answer.
 */

import type { ModelView } from "@/components/models/contract";

import { FailureReasons } from "./FailureReasons";
import { FitQualityChart } from "./FitQualityChart";
import { PanelLattice } from "./PanelLattice";

const view: ModelView = {
  model: "panel_fit",
  Signature: PanelLattice,
  charts: [
    {
      id: "fit-quality",
      title: "Fit quality",
      caption:
        "One dot per fitted group, with the running median R-squared over them. Higher is better here — the opposite of the forecasting metric.",
      Chart: FitQualityChart,
    },
    {
      id: "failure-reasons",
      title: "Why groups failed",
      caption:
        "The four reasons are a closed set, so a zero is an answer. A failed group is recorded with its reason, never dropped.",
      Chart: FailureReasons,
    },
  ],
  honesty:
    "The lattice is structure, not decoration: one cell per group where the panel is small enough " +
    "for that, a stated proportion where it is not, and the line under it says which of the two you " +
    "are looking at. The fitted, failed and pending blocks are the real counts off every progress " +
    "message, and the pulsing cell is the group being fitted right now — nothing in here runs on a " +
    "timer or drifts while idle. Two qualifications. The blocks are contiguous, so a cell's POSITION " +
    "is a proportion and never says which unit failed; the lattice does not know that and must not " +
    "imply it. And when the run ends the lattice becomes one flat colour, because no cell means " +
    "anything different from any other cell once nothing is being fitted. The split itself does not " +
    "go away with it: the outcome bar and the numbers below the lattice are data rather than " +
    "animation, they read the same in every state including after the run is over, and a run that " +
    "SUCCEEDED with a third of its groups failed still says so there. The synthetic-panel banner is " +
    "read from the run's own provenance fields, not inferred — the default table does not exist yet, " +
    "so the default run really is generated data. Both charts below are entirely real.",
};

export default view;
