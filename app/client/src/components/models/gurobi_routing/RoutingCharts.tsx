/**
 * Routing's diagnostics, which are scheduling's diagnostics with one label
 * changed.
 *
 * Both models are sampled by `job/drivers/gurobi.py`, so the series are
 * byte-identical; the only thing that differs is what the objective MEANS.
 * A wrapper is cheaper than a second chart implementation that would have to
 * be kept in step with the first.
 */

import type { ModelViewProps } from "../contract";
import { MipConvergenceChart } from "../gurobi_shared/GurobiCharts";

export function RoutingConvergenceChart(props: ModelViewProps) {
  return <MipConvergenceChart {...props} objectiveLabel="route cost" />;
}
