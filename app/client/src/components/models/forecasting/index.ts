/**
 * The `forecasting` model view.
 *
 * Signature: a horizon timeline in plain SVG, NOT the Three.js neural net the
 * design digest sketches — see the header of `ForecastingSignature.tsx` for
 * the reasoning and for what it would take to go back.
 *
 * Charts: the two the digest names, both always visible rather than behind a
 * disclosure. They draw from different sources at different moments (live
 * `progress` versus one `result` at the end), which is the thing the pairing
 * is meant to make obvious.
 */

import { FORECASTING } from "@/lib/models";
import type { ModelView } from "../contract";
import { ForecastRevealChart } from "./ForecastRevealChart";
import { ForecastingSignature } from "./ForecastingSignature";
import { TrainingLossChart } from "./TrainingLossChart";

const forecastingView: ModelView = {
  model: FORECASTING.name,
  Signature: ForecastingSignature,
  charts: [
    {
      id: "training-loss",
      title: "Training loss",
      caption:
        "Live, one point per epoch. train_loss comes from payload; val_loss is primary_metric, not a payload key.",
      Chart: TrainingLossChart,
    },
    {
      id: "forecast-reveal",
      title: "Forecast reveal",
      caption:
        "From results() when the run finishes. The ribbon is ± val_mae — a constant-width error band, not a prediction interval.",
      Chart: ForecastRevealChart,
    },
  ],
  honesty:
    "Real in the animation: how many horizon markers are lit, which tracks percent_complete (100 × (epoch + 1) / epochs), and the single flat colour a finished run freezes into. Decorative: the history waveform, the projected path the markers sit on, the width of the shaded band, and the sweep that crosses the forecast region while a run is live. The band narrows with elapsed progress, not with uncertainty — this model emits no prediction interval of any kind — and the sweep is pacing, tied to no epoch and no step. Nothing in the animation encodes the loss. Both charts below are entirely real: training loss from live progress messages, the forecast from results().",
};

export default forecastingView;
