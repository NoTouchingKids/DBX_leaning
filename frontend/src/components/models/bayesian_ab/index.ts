/**
 * The `bayesian_ab` model view.
 *
 * Everything here is built around one property of the model: it is
 * closed-form and finishes in milliseconds, so the client will routinely see
 * a terminal status with an empty `snapshot.progress`. Nothing in this view
 * depends on catching an intermediate state — the signature derives its
 * stages from the status when no progress arrived, and both charts fall back
 * to the `result` preview rows for the same numbers.
 */

import type { ModelView } from "@/components/models/contract";

import { BayesianAbSignature } from "./BayesianAbSignature";
import { DecisionChart } from "./DecisionChart";
import { PosteriorChart } from "./PosteriorChart";

const view: ModelView = {
  model: "bayesian_ab",
  Signature: BayesianAbSignature,
  charts: [
    {
      id: "posteriors",
      title: "Posteriors",
      caption:
        "Both arms' Beta posteriors, drawn client-side from posterior_alpha and posterior_beta.",
      Chart: PosteriorChart,
    },
    {
      id: "decision",
      title: "Decision",
      caption:
        "P(B>A), both expected losses, and the lift interval against zero. The rule consults probability and loss together.",
      Chart: DecisionChart,
    },
  ],
  honesty:
    "Which of the five chips are filled is real: it comes from stage_index " +
    "when a progress message arrives and, when none does, from the terminal " +
    "status — a SUCCEEDED run necessarily ran all five stages. The arm labels " +
    "and the decision word are quoted verbatim from the payload or, if the run " +
    "outran the stream, from the result rows. What is not real is the pacing, " +
    "and the implication that there is any: this model is closed-form and " +
    "finishes in milliseconds, so the cascade across the chips is a fixed 70ms " +
    "stagger that exists only to show the order the stages run in, and the " +
    "normal case is all five arriving at once. Nothing in the panel is " +
    "positioned or sized by a numeric value — the numbers are in the two " +
    "charts below.",
};

export default view;
