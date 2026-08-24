/**
 * The nine model views, by model name.
 *
 * The single place a `ModelView` is bound to a model — deliberately, so that
 * `ModelPage` resolves one lookup and knows nothing about any particular
 * model, and so that adding a tenth is one import and one entry.
 *
 * Static imports, not `import()` by name. A dynamic path would defer nine
 * chunks and cost a loading state on every model page, and the whole set is
 * small; more importantly a name-built import path cannot be typechecked, so
 * a renamed directory would fail at runtime on one page instead of at build
 * time everywhere.
 *
 * `viewFor` returns `undefined` rather than throwing. A model registered in
 * `MODEL_SPECS` with no view yet is a real and supported state: the generic
 * run page is correct for every model, which is why it was built first.
 */

import type { ModelView } from "./contract";

import annealing from "./annealing";
import bayesianAb from "./bayesian_ab";
import forecasting from "./forecasting";
import gurobiRouting from "./gurobi_routing";
import gurobiScheduling from "./gurobi_scheduling";
import mcmc from "./mcmc";
import neuralNet from "./neural_net";
import scenario from "./scenario";
import streamingResults from "./streaming_results";

export const MODEL_VIEWS: readonly ModelView[] = [
  gurobiScheduling,
  gurobiRouting,
  scenario,
  forecasting,
  mcmc,
  bayesianAb,
  annealing,
  neuralNet,
  streamingResults,
];

const BY_NAME = new Map(MODEL_VIEWS.map((view) => [view.model, view]));

export function viewFor(model: string | undefined): ModelView | undefined {
  return model === undefined ? undefined : BY_NAME.get(model);
}
