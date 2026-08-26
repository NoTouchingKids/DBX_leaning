/**
 * `scenario` — the exhaustive sweep.
 *
 * Cheap, fast and numerous by design: this is the model that exercises
 * fan-out against Free Edition's five-concurrent-task ceiling, so a run can
 * be over in seconds. Nothing in this view waits for a long RUNNING phase to
 * become useful — the sweep grid reads correctly with a single progress
 * message, and the completion chart is built from the result preview, which
 * arrives whether or not anyone watched the run happen.
 */

import type { ModelView } from "@/components/models/contract";

import { CurrentlyEvaluating } from "./CurrentlyEvaluating";
import { ObjectiveAcrossScenarios } from "./ObjectiveAcrossScenarios";
import { ScenarioSweep } from "./ScenarioSweep";

const view: ModelView = {
  model: "scenario",
  Signature: ScenarioSweep,
  charts: [
    {
      id: "currently-evaluating",
      title: "Currently evaluating",
      caption:
        "last_scenario and last_outcome, straight off the progress payload — the last member of each batch, not a per-scenario feed.",
      Chart: CurrentlyEvaluating,
    },
    {
      id: "objective-across-scenarios",
      title: "Objective across scenarios",
      caption:
        "Every evaluated scenario's objective, drawn once from the result preview when the run ends.",
      Chart: ObjectiveAcrossScenarios,
    },
  ],
  honesty:
    "The scan cursor is filled once the run has reported a scenario and outlined until then — " +
    "outlined, it is parked on the first cell as a spin-up frame and claims nothing. Where the " +
    "filled cursor sits is real: the demand x capacity cell of the last scenario the run reported, " +
    "matched from that scenario's own multipliers, with that scenario's row and column labels lit. " +
    "The amber cell marks a genuine best_objective improvement, and its two forms are the " +
    "difference between knowing and guessing where — filled means the improving scenario is the one " +
    "the message named, dashed means the cell is an approximation, either because the improvement " +
    "happened somewhere inside a batch or because the run's grid is not the one drawn here. The " +
    "grid is 6x4 because the third dimension, unit cost, is swept inside each cell — every cell is " +
    "visited three times, not once. Drawn " +
    "rather than measured: progress is batched about every ten scenarios, so the cursor jumps, and " +
    "both the brief left-to-right cascade behind a jump and the cursor's slow pulse between jumps " +
    "exist to keep a live run distinguishable from a stalled one — the pulse is not a scenario " +
    "being evaluated, only the run still being open. Because a model view is never handed the run's " +
    "config, the axes are the default multipliers, so a run with a custom grid matches nothing and " +
    "its marks are placed at its completed fraction of the sweep instead — real progress, but not a " +
    "real coordinate, and the axis labels stay unlit to say so. Cells carry four discrete states " +
    "and the cursor sits on top of them; nothing anywhere is a magnitude. When the run ends the " +
    "whole grid goes to one flat colour, the legend is withdrawn, and nothing means anything cell " +
    "by cell; the numbers are in the two cards below.",
};

export default view;
