/**
 * `streaming_results` — the incremental-results case.
 *
 * The only model in the platform that emits `result` more than once per run,
 * and the reason the envelope has `chunk_index` and `final` at all. Its
 * results arriving live, mid-run, is the entire point of the model, so they
 * are the always-visible chart rather than a collapsed disclosure — deferring
 * them behind a click would mean not showing the model.
 *
 * One chart, not two. The growing predicted-vs-actual series carries both the
 * progress story (it extends every time a window completes) and the results
 * story (the two lines) at once.
 */

import type { ModelView } from "@/components/models/contract";

import { PredictedVsActual } from "./PredictedVsActual";
import { RollingWindow } from "./RollingWindow";

const view: ModelView = {
  model: "streaming_results",
  Signature: RollingWindow,
  charts: [
    {
      id: "predicted-vs-actual",
      title: "Predicted vs actual",
      caption:
        "Grows as result chunks land — one chunk per completed backtest window, appended and never replaced.",
      Chart: PredictedVsActual,
    },
  ],
  honesty:
    "The window's position here is state rather than decoration. It is placed from windows_done " +
    "and moves only when a backtest window actually finished and its chunk of results went out — " +
    "never on a timer, and never as idle drift. How many segments stand at full height is the " +
    "same count, read the same way. Three qualifications. The track is a fixed twelve segments " +
    "while the real window count comes from the run's config — at the defaults there are roughly " +
    "thirty of them — so the step is proportional, and the line under the track says whether you " +
    "are getting one step per window or several windows per step. The window's footprint, and its " +
    "split into a fit region and the forecast horizon ahead of it, is drawn rather than measured: " +
    "neither the window size nor the horizon is in the progress payload this animation reads. And " +
    "the pale bar sweeping across the window while the run is live is pacing on a fixed loop, so " +
    "that a live run looks different from a stalled one; it is not tied to a window, a row, or " +
    "anything the job reports. When the run ends the track becomes one flat colour at one height " +
    "and the window fades out, because a stopped window has no position left to report. The chart " +
    "below is not part of the animation at all: those are the real predicted and actual rows out " +
    "of the result chunks.",
};

export default view;
