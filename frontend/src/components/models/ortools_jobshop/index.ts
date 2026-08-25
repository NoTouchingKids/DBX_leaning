/**
 * `ortools_jobshop` — the CP-SAT model, and the one that exists to contrast
 * with the two Gurobi views.
 *
 * The contrast is the design brief for this page. Same telemetry shape —
 * incumbent, bound, gap — arriving through a different mechanism, and three
 * differences that the view has to make legible rather than smooth away:
 *
 *  - **Improvements are rare and meaningful.** A MIP callback fires
 *    constantly; CP-SAT's solution callback fires only on an improving
 *    solution, a handful of times across a whole run. So a still signature
 *    here means "not improving", where a still Gurobi grid would be ambiguous.
 *  - **`percent_complete` is real** — and is a clock, not a search fraction.
 *    See `SolverClockBar`.
 *  - **The gap is `relative_gap`, not `mip_gap`.** Same formula, different
 *    solver, and the label is the model's own choice. Hence the charts here
 *    are a duplicate of the Gurobi pair rather than a reuse of it; see the
 *    header of `JobshopCharts.tsx`.
 *
 * Like `gurobi_scheduling`, this model needs no separate results disclosure:
 * its terminal frame IS the results view, because `results_ortools_jobshop` is
 * one row per scheduled operation and a Gantt is that table drawn.
 */

import type { ModelView } from "../contract";
import { CpSatConvergenceChart, ImprovementChart } from "./JobshopCharts";
import { MachineGantt } from "./MachineGantt";

const view: ModelView = {
  model: "ortools_jobshop",
  Signature: MachineGantt,
  charts: [
    {
      id: "cpsat-convergence",
      title: "CP-SAT convergence",
      caption:
        "Best makespan in minutes against the best proven bound, with relative_gap on the right axis — the model's own label, deliberately not mip_gap, because this is a constraint-programming search rather than a MIP. The makespan line starts only when the first feasible schedule is found.",
      Chart: CpSatConvergenceChart,
    },
    {
      id: "improvement-steps",
      title: "Improvements and conflicts",
      caption:
        "solutions_found steps up once per improving solution — typically a handful of times across a whole run, not continuously — and each dot is a sample reporting one. conflicts is CP-SAT's learned-clause count on a log axis; it stops before the last sample because the final, post-solve sample carries no callback counters at all.",
      Chart: ImprovementChart,
    },
  ],
  honesty:
    "The machine lanes are this run's real shop floor: the machine count and names come from the model's own instance log line, and the instance size is restated in every progress message. While the run is going, the bars are DECORATIVE — no progress message carries a single operation's start time, because the schedule is read out of the solver only once, after the search returns, so there is nothing real to draw until then. What is real is the cadence: the floor redraws exactly once per observed improvement in solutions_found, and CP-SAT's callback fires only on an improving solution — a handful of times across a whole run rather than continuously — so a still floor means the search genuinely is not improving, not that the page has stalled. The clock bar below the lanes is real but is a TIME fraction, not a search fraction: it is elapsed solver time against the configured limit, so 90% means the clock is nearly up and says nothing about how much search is left. The terminal frame is not decorative — on a succeeded or cancelled run every bar is a real scheduled operation from the result rows, at its real start minute and duration, and where the preview was downsampled the view says how many of the written operations it is showing and withholds machine utilisation rather than understating it.",
};

export default view;
