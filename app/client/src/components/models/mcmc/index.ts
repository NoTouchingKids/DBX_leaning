/**
 * The `mcmc` model view.
 *
 * Two charts, which is the contract's maximum, and both are real telemetry:
 * the trace answers "are these chains the same distribution?" and chain
 * health answers "is one of them unwell?". The results disclosure is
 * deliberately not overridden — `RunWorkspace`'s generic one is already a
 * collapsed preview of `results()`, which for this model is the per-parameter
 * summary rows plus the thinned `draws_sample`, and that is exactly what the
 * design asks for. A model-specific one would be a second implementation of
 * the same disclosure.
 */

import type { ModelView } from "@/components/models/contract";

import { ChainHealthChart } from "./ChainHealthChart";
import { McmcSignature } from "./McmcSignature";
import { TraceChart } from "./TraceChart";

const view: ModelView = {
  model: "mcmc",
  Signature: McmcSignature,
  charts: [
    {
      id: "trace",
      title: "Live trace",
      caption:
        "Each chain's current position, accumulated from the per-message snapshots this tab received.",
      Chart: TraceChart,
    },
    {
      id: "chain-health",
      title: "Chain health",
      caption:
        "Acceptance fraction per chain. emcee has no divergences, so stuck_chains is the equivalent diagnostic.",
      Chart: ChainHealthChart,
    },
  ],
  honesty:
    "The number of dots is the run's chain count, up to sixteen; before the " +
    "first progress message arrives there is no count yet and eight " +
    "placeholder dots are drawn, which is why the chains figure above reads " +
    "as absent rather than as eight. How far the ensemble spreads from the " +
    "centre is the only thing in this panel driven by data: it is max_rhat " +
    "mapped onto a radius, so the cloud draws in as the chains converge and " +
    "stays wide while they have not. Everything else is decoration — the " +
    "dashed contours are a fixed reference, not a posterior, each dot sits at " +
    "a seeded offset and wanders on a slow decorative drift rather than " +
    "tracking that chain's parameters, and where the soft halos pile up is " +
    "dots overlapping, not a density. The real per-chain positions are the " +
    "trace chart below. On a finished run the drift stops, the ensemble comes " +
    "to rest on one ring and every dot takes the run's outcome colour, with " +
    "no per-chain meaning surviving.",
};

export default view;
