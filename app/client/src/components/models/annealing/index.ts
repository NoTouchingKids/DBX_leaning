/**
 * The `annealing` per-model view.
 *
 * Simulated annealing over a knapsack: which trips a driver accepts inside a
 * fixed shift. It is the only model here whose live numbers deliberately get
 * worse, and everything in this directory is arranged around not letting that
 * read as a failure — see `SearchTraceChart` for the chart that does the
 * work, `series.ts::shiftUsage` for the tone `feasible: false` is allowed to
 * take, and `AnnealingSignature` for the cooling animation.
 *
 * Two charts, which is the contract's maximum, and the second one is not
 * padding: the signature claims its extent and its palette are set by the real
 * temperature, and the cooling chart is where that claim can be checked. Note
 * what it does NOT claim — the lattice's cadence is a fixed 2.4s and has
 * nothing to do with the temperature. Pacing the flicker by heat is the thing
 * that got removed, because it strobed.
 */

import type { ModelView } from "@/components/models/contract";
import { ANNEALING } from "@/lib/models";

import { AnnealingSignature } from "./AnnealingSignature";
import { CoolingChart } from "./CoolingChart";
import { SearchTraceChart } from "./SearchTraceChart";

const ANNEALING_VIEW: ModelView = {
  // Taken from the spec rather than retyped: `RunWorkspace` looks the view and
  // the spec up separately and expects the names to match.
  model: ANNEALING.name,
  Signature: AnnealingSignature,
  charts: [
    {
      id: "search-trace",
      title: "Search trace",
      caption: "current objective falls on purpose; best fare is what is kept",
      Chart: SearchTraceChart,
    },
    {
      id: "cooling",
      title: "Cooling schedule",
      caption: "geometric cooling is a straight line on the log axis",
      Chart: CoolingChart,
    },
  ],
  honesty:
    "how much of the lattice moves, how deep the settled block at the bottom is, and how warm the colours are. All three are derived from the run's own temperature, normalised against the geometric cooling schedule the model reports — a hot search agitates the whole field in warm tones, a cold one has crystallised almost to the top and changes a single cell at a time in blue — and the four readouts under the grid are read straight off the latest progress message. Decorative: which cells are lit, and the rhythm. The lattice turns over on a fixed 2.4-second cycle whatever the temperature, because pacing the flicker by temperature strobed at the hot end. The payload carries nothing per-trip, so the pattern is a random walk over a fixed grid rather than the shift being assembled: the settled block's depth is the run's position on its cooling schedule and not a count of anything, and the trips actually chosen exist only in the results table. When the run ends the whole grid takes one flat colour for the outcome and stops, and no cell means anything different from any other at that point.",
};

export default ANNEALING_VIEW;
