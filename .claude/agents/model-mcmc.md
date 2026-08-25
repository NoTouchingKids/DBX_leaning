---
name: model-mcmc
description: Design record for models/mcmc/ (BUILT) — a Bayesian model fit via MCMC sampling. The best stress test of the streaming path in this platform (long-running, high-frequency progress telemetry with a genuinely different shape — chains/draws/divergences/r-hat).
tools: Read, Write, Edit, Bash, Grep, Glob
---

## Status: built. This is the design record, not a to-do list.

`models/mcmc/` exists, is tested under `tests/models/`, and is registered in
`pyproject.toml`, `deploy/requirements/`, `resources/model_mcmc.job.yml`,
`resources/app.yml` and `uc_ddl/002_model_results.sql`. Read the source and
its tests before this file — the module docstring is maintained, this brief
is not. It is kept for the reasoning: **why** this model is in the lineup,
which is the thing the code cannot say about itself.

Two facts this brief predates and every one of these briefs got wrong:

- **This model reads real data.** All eleven models load
  `samples.nyctaxi.trips` through `models/_data`, falling back to a
  deterministic generator when there is no workspace, and carry
  `data_source` / `data_synthetic` / `data_rows` / `data_fallback_reason` on
  every result row so the two runs stay distinguishable afterwards. Where
  this file says "synthetic" or "small fixed problem", read "synthetic
  fallback".
- **There are eleven models, not five.** Any count below is stale. The other
  six were built from `models/README.md` and `/new-model` with no brief at
  all, deliberately — see `docs/parallelization-plan.md`.

This brief was written to build `models/mcmc/`. Read `CLAUDE.md` and
`docs/message-envelope-spec.md` before writing anything.

## What this model is, and why it's in the lineup

A Bayesian model (pick something genuinely small and fast to keep dependency
weight down — a hierarchical regression or similar is plenty) fit via MCMC
sampling. **This is the model most likely to actually stress the streaming
path** — long sampling runs, multiple chains, and a progress shape
(divergences, r-hat, effective sample size) that looks like nothing else in
this platform. If the message envelope and the transport can handle this
model's telemetry cleanly, they can handle the rest of the lineup.

Prefer a lightweight sampling library. `emcee` is the lightest dependency if
a full probabilistic-programming framework isn't needed; PyMC or NumPyro are
reasonable if the model benefits from a proper PPM (priors, likelihood
composition) — pick based on how much modelling flexibility is actually
useful here, not on which is most impressive. Given this model's job is to
stress the *platform*, not showcase Bayesian modelling, lean toward the
lighter option unless there's a real reason not to.

## Duck-typed surface (no base class, no inheritance)

Same convention as every model here — see `docs/architecture.md`.

## Progress — this is where `payload` earns its keep

MCMC progress genuinely doesn't fit `percent_complete`+`primary_metric`
alone — most of what matters is chain-specific and needs `payload`:

- `percent_complete` — draws completed / total draws (across chains, or per
  chain — pick one and be consistent), if the sampler makes this knowable
  up front (fixed draw count) — often it is
- `primary_metric` — max r-hat across parameters (a convergence diagnostic
  that's meaningfully "one number to watch"); `primary_metric_label` —
  `"max_rhat"`
- `payload` — per-chain detail: divergence count, current chain index,
  whatever else a richer trace-plot view would want

Sample progress at a sane cadence relative to draw rate — if draws happen
much faster than a human can watch, emit progress every N draws or every
couple of seconds, whichever comes first, not every single draw.

## Cancellation

Check the harness's cancellation signal between draws (or between chain
batches, if the sampler doesn't checkpoint per-draw easily). On
cancellation, keep whatever samples have been drawn so far as results — a
partial posterior is still a usable (if less certain) result, consistent
with every other model's "cancellation keeps what exists" rule.

## Results

Posterior samples (or a summary of them — check with whoever's consuming
this whether raw draws or summary statistics per parameter are more useful;
if genuinely unsure, write both: summary stats as the primary result rows,
raw draws only if storage cost is acceptable) as plain dicts.

## Explicit non-goals

- No WebSocket, HTTP, Delta, or SQL code — only `emit(...)`.
- No knowledge of `run_id`/`seq`/timestamps.
- Don't reach for a heavyweight PPL by default — start light, upgrade only
  if the model genuinely needs it.

## Tests to write

- Runs standalone against a small fixed problem, produces samples with sane
  shape and at least approximately correct posterior (a known-analytic
  toy problem is a good check here).
- Cancellation mid-sampling still produces usable partial results.
- Progress messages populate `payload` with per-chain diagnostics without
  breaking the generic `percent_complete`/`primary_metric` fields a
  non-MCMC-aware view would use as a fallback.
