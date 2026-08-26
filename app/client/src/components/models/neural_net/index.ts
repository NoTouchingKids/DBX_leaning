/**
 * The `neural_net` model view.
 *
 * No design doc covers this model — the digest predates it — so both the
 * signature and the charts are derived from the same principle in
 * `../contract.ts` and from what `job/models/neural_net/model.py` emits.
 *
 * The three facts that shaped it:
 *
 *  1. Progress arrives at two levels on one stream. The charts are keyed on
 *     the model's own batch-step counter rather than on `epoch`, and epoch
 *     boundaries are marked. See `series.ts`.
 *  2. `primary_metric` is `val_accuracy` and improves UPWARD — the opposite
 *     of `forecasting`. Nothing that encodes that direction is shared between
 *     the two directories. See `metric.ts`.
 *  3. Accuracy on ~55/30/15 classes means nothing without the majority-class
 *     baseline, so the baseline shares the accuracy chart's axis rather than
 *     being a footnote.
 */

import { NEURAL_NET } from "@/lib/models";
import type { ModelView } from "../contract";
import { AccuracyChart } from "./AccuracyChart";
import { LossChart } from "./LossChart";
import { NeuralNetSignature } from "./NeuralNetSignature";

const neuralNetView: ModelView = {
  model: NEURAL_NET.name,
  Signature: NeuralNetSignature,
  charts: [
    {
      id: "accuracy-vs-baseline",
      title: "Accuracy vs baseline",
      caption:
        "val_accuracy (primary_metric) and macro_f1 against the majority-class baseline. best_val_accuracy is null until the first epoch ends — that gap is real.",
      Chart: AccuracyChart,
    },
    {
      id: "loss-both-levels",
      title: "Loss, both levels",
      caption:
        "Keyed on the model's batch-step counter, not on epoch — both progress levels arrive on one stream. Dashed verticals are epoch boundaries.",
      Chart: LossChart,
    },
  ],
  honesty:
    "Real in the animation: the filled epoch cells, which come only from level: \"epoch\" messages, and the batch marker under the cell in flight, which is payload.batch / batches_per_epoch from level: \"batch\" ones — two tiers because two levels genuinely arrive interleaved on one stream. Real too: four inputs and three output classes are fixed constants of the model, and the single flat colour a finished run freezes into. Decorative: the two middle columns of nodes — hidden layer widths are config and never reach the payload, so those counts are a sketch, not this run's architecture — and the propagation loop over the network. Which elements that loop lights is honest (activations light the units, gradients light the weights, forward then back) but its timing is not: it runs at a fixed cadence, not one pulse per batch or epoch the job reports, and the warm-up that plays once as a run starts is a fixed animation rather than a sign that anything has begun computing. Nothing in the animation encodes accuracy; accuracy here only means something next to the majority-class baseline, which is drawn with it on the chart.",
};

export default neuralNetView;
