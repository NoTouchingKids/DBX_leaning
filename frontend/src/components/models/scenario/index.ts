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
    "The scan head is real: it sits on the demand x capacity cell of the last scenario the run " +
    "reported, matched from that scenario's own multipliers, and the amber cell marks a genuine " +
    "best_objective improvement. The grid is 6x4 because the third dimension, unit cost, is swept " +
    "inside each cell — every cell is visited three times, not once. Drawn rather than measured: " +
    "progress is batched about every ten scenarios, so the head jumps, and the brief left-to-right " +
    "cascade between jumps exists only to keep the jump legible; the head resting on the first cell " +
    "while a run is starting is a spin-up frame, not a report; and because a model view is never " +
    "handed the run's config, the axes are the default multipliers, so a run with a custom grid " +
    "places its head at its completed fraction of the sweep instead — real progress, but not a real " +
    "coordinate. Cells are four discrete states and never a magnitude. When the run ends the whole " +
    "grid goes to one flat colour and stops meaning anything cell by cell; the numbers are in the " +
    "two cards below.",
};

export default view;
