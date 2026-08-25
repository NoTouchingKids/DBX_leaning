/**
 * `gurobi_routing` — capacitated vehicle routing with lazy capacity cuts.
 *
 * Same solver, same driver and therefore the same two diagnostics charts as
 * `gurobi_scheduling`; the labels differ because the objective means something
 * different (distance-weighted route cost, not staffing cost against roster
 * preference). The signature is what separates the two models on sight.
 *
 * There is no third chart for `cuts_added` / `separation_calls`, even though
 * they are the most interesting numbers this model produces: they reach the
 * results and the DEBUG logs and never a `progress` message. A chart for them
 * would have to invent them.
 */

import type { ModelView } from "../contract";
import { SearchActivityChart } from "../gurobi_shared/GurobiCharts";
import { RouteFan } from "./RouteFan";
import { RoutingConvergenceChart } from "./RoutingCharts";

const view: ModelView = {
  model: "gurobi_routing",
  Signature: RouteFan,
  charts: [
    {
      id: "mip-convergence",
      title: "MIP convergence",
      caption:
        "incumbent route cost and best_bound closing on each other, mip_gap on the right axis. The bound moves as lazy capacity cuts are separated, so the gap can widen between samples — it is not a monotone line.",
      Chart: RoutingConvergenceChart,
    },
    {
      id: "search-activity",
      title: "Search activity",
      caption:
        "nodes_explored on a log axis. Each dot is a new incumbent — a candidate routing Gurobi accepted because separation found no violated capacity cut, which is also what re-links the fan above.",
      Chart: SearchActivityChart,
    },
  ],
  honesty:
    "The number of stops and vehicles is this run's real instance, read from the model's own input log. Where the stops sit, and which tour each one joins, is DECORATIVE while the run is live: no progress message carries coordinates or a candidate routing, only aggregate solve metrics. What is real is the cadence — the fan re-links exactly once per new incumbent, and every incumbent in this model is a set of routes that passed separation with no violated capacity cut. The terminal frame is not decorative: on a succeeded or cancelled run the tours are the actual routes from the result rows, drawn from each stop's own coordinates with the depot at the origin.",
};

export default view;
