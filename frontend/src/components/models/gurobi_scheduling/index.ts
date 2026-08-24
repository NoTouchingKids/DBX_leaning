/**
 * `gurobi_scheduling` — the model the signature-animation pattern was designed
 * against.
 *
 * Two diagnostics cards, both fed entirely by `progress` messages, and one
 * signature that doubles as the results view: this is the model with no
 * separate results disclosure, because a solved schedule and a live schedule
 * grid are the same picture.
 */

import type { ModelView } from "../contract";
import { MipConvergenceChart, SearchActivityChart } from "../gurobi_shared/GurobiCharts";
import { ScheduleGrid } from "./ScheduleGrid";

const view: ModelView = {
  model: "gurobi_scheduling",
  Signature: ScheduleGrid,
  charts: [
    {
      id: "mip-convergence",
      title: "MIP convergence",
      caption:
        "incumbent and best_bound closing on each other, mip_gap on the right axis. Built from accumulated progress messages, not from results — and the incumbent line starts only when the first feasible schedule is found.",
      Chart: MipConvergenceChart,
    },
    {
      id: "search-activity",
      title: "Search activity",
      caption:
        "nodes_explored on a log axis, because it crosses orders of magnitude in a minute. Each dot is a sample that reported a new incumbent — the same events that step the grid above.",
      Chart: SearchActivityChart,
    },
  ],
  honesty:
    "The grid's dimensions are this run's real staff x day instance, read from the model's own build log. Which cells light up while the run is going is DECORATIVE: no progress message carries per-cell or candidate-schedule data, only aggregate solve metrics. What is real is the cadence — the grid changes exactly once per new incumbent (solution_count increment) and sits still when the solver is not improving, so a frozen grid means a stalled search, not a stalled page. The terminal frame is not decorative: on a succeeded or cancelled run the grid is the actual schedule from the result rows, one filled cell per assigned shift.",
};

export default view;
