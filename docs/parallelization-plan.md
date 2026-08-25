# Parallelization plan

How to actually run multiple Claude Code sessions against this repo without
them colliding, and in what order.

**Where this stands.** The sequencing below has been run: the probes cleared
(`docs/spike-results.md`), `shared/` landed and is frozen, and `job/`, `app/`
and eleven models are built and tested. What is still live is the *method* —
the file-disjointness rule, the shared-file warning about `pyproject.toml` and
`uv.lock`, and the "freeze the contract before you fan out" discipline, which
the frontend applied again for its own per-model views. Read the track table
below as the record of how the backend got built, not as a queue of work
waiting to start.

## The one thing that can't be parallel

`shared/` — the message envelope implementation — has to exist, and be
frozen, before the model tracks and the transport tracks start in parallel.
Every other track depends on its shape. Building several things against a
contract that's still moving means rework, not speed.

The same rule has since been applied a second time, on the frontend:
`app/client/src/components/models/contract.ts` was frozen before the per-model
views were fanned out, for exactly this reason. When a fan-out is coming, find
the contract it shares and freeze that first.

Practical sequencing:

```
1. /spike-ws and /spike-sse           (sequential, gates everything)
2. shared/ (the envelope, from docs/message-envelope-spec.md)  (sequential)
3. Everything below                    (parallel, one worktree per track)
```

## Tracks and their agent briefs

Each track has a brief in `.claude/agents/`. A track = one worktree = one
branch = one Claude Code session.

| Track | Agent brief | Depends on |
|---|---|---|
| `job/` harness (WS client, HTTP push, Delta writer, model loader) | `.claude/agents/transport-job.md` | `shared/` |
| `app/` (FastAPI, SSE, ServiceHub, whoami) | `.claude/agents/transport-app.md` | `shared/` |
| `models/gurobi_scheduling/` | `.claude/agents/model-gurobi-scheduling.md` | `shared/` (message shape only — a model never imports the job or app) |
| `models/scenario/` | `.claude/agents/model-scenario.md` | `shared/` |
| `models/forecasting/` | `.claude/agents/model-forecasting.md` | `shared/` |
| `models/mcmc/` | `.claude/agents/model-mcmc.md` | `shared/` |
| `models/streaming_results/` | `.claude/agents/model-streaming-results.md` | `shared/` |
| `models/annealing/`, `models/bayesian_ab/`, `models/gurobi_routing/`, `models/neural_net/` | none — built after the pattern was established, from `models/README.md` and `/new-model` | `shared/` |
| `models/ortools_jobshop/` — CP-SAT job shop, the open-source counterweight to the two Gurobi models: no licence file, no expiry, no size cap | none — same route, and later still | `shared/` |
| `models/panel_fit/` — many small per-group fits, and the one model whose individual units may FAIL while the run SUCCEEDS | none — same route, and later still | `shared/` |
| `app/client/` | `.claude/agents/frontend.md` | Explicitly low priority — start after `app/` + `job/` + one model work end to end |

There are **eleven** models on disk, not the five that got their own brief.
`models/` on disk, `[tool.dbx-leaning.models]` in `pyproject.toml`, and
`resources/model_*.job.yml` all have to agree; they are cross-checked in
`tests/deploy/`. A brief per model turned out to be worth writing only while
the contract was still being discovered — once `models/README.md` and
`/new-model` existed, the later six needed no brief at all, and that is the
signal to stop writing them rather than a gap to fill in. Six is now enough
of a run to call it settled rather than lucky, and the last two are the
better evidence: `ortools_jobshop` and `panel_fit` arrived when there was
considerably more surrounding platform to get wrong than there had been for
the middle four — a registry in `pyproject.toml`, a job file, a generated
requirements file, a results-table DDL — and `models/README.md` covered all
of it.

One brief describes a model that was never built:
`.claude/agents/model-nyctaxi-demand.md`, the Spark-native aggregation
proposed in `docs/model-expansion-and-packaging.md`. Its original
justification is gone — nine of the eleven models read
`samples.nyctaxi.trips` through `models/_data`, and the other two read
elsewhere in Unity Catalog (`ortools_jobshop` builds its instance from
`samples.bakehouse.sales_transactions`; `panel_fit` asks for a panel table
nobody has landed and falls back to its generator) — so if it is ever built
it is on the strength of its telemetry shape, not on closing a gap. See that
document.

The `models/*` tracks are the cleanest parallel case: same contract, zero
file overlap, no dependency on each other or on `job`/`app` internals (a
model only ever touches the `emit()` callback it's handed). They can run
concurrently with `job/` and `app/` from the moment `shared/` lands.

## Running it — two ways

**A. Multiple terminals, one Claude Code session per worktree (recommended
for real parallelism).**

```bash
git worktree add ../DBX_leaning-job         -b feat/transport-job
git worktree add ../DBX_leaning-app         -b feat/transport-app
git worktree add ../DBX_leaning-gurobi      -b feat/model-gurobi-scheduling
git worktree add ../DBX_leaning-scenario    -b feat/model-scenario
git worktree add ../DBX_leaning-forecasting -b feat/model-forecasting
git worktree add ../DBX_leaning-mcmc        -b feat/model-mcmc
git worktree add ../DBX_leaning-streaming   -b feat/model-streaming-results
```

Then, in each worktree, in its own terminal tab:

```bash
cd ../DBX_leaning-gurobi
claude "Read .claude/agents/model-gurobi-scheduling.md and CLAUDE.md, then build this track."
```

Each session only ever touches its own worktree's files (plus reads from
`shared/` and `docs/`, which don't change), so there's no file contention.
Merge each branch back via PR once its track's own tests pass.

**B. One orchestrating session, using Claude Code's own subagent/Task
tool.** If you'd rather not juggle terminals, a single Claude Code session
in the main repo can dispatch each track as a subagent task, pointing it at
the same `.claude/agents/*.md` brief and, if the tracks write to different
directories (`job/`, `app/`, `models/x/`), this is safe without worktrees —
they're disjoint files. Worktrees start to matter more once a track's work
might touch shared files (e.g. a shared requirements/lockfile) — see
`/new-model` for the one common shared-file collision (adding a new model to
`models/` doesn't touch anything another model track touches, but adding a
dependency to a shared `pyproject.toml` does; if that comes up, do it as a
separate, sequential step, not inside a parallel track).

Concretely, that shared file is now `pyproject.toml` **and `uv.lock`**. A
track adding a dependency runs `uv add --optional <extra> <package>`, which
rewrites both — so it is a merge conflict waiting to happen across parallel
worktrees, and belongs in its own sequential commit that every other track
then syncs onto (`uv sync`) before its next test run.

Pick A if you want true wall-clock parallelism and don't mind multiple
terminals. Pick B if you'd rather stay in one place and are fine with the
subagent tool's own scheduling.

## Merge order

1. `shared/` first (already sequential, already merged before tracks start).
2. `job/` and `app/` next — they don't depend on any specific model, but a
   model track needs *something* to run against for integration testing, so
   land these before the model PRs merge (the model PRs can still be
   *developed* in parallel against `shared/` alone; they just don't get
   integration-tested until `job/`+`app/` land).
3. One model — `models/gurobi_scheduling/` is the natural first pick, since
   it was the original driving use case — merges next and becomes the first
   end-to-end vertical slice.
4. The remaining model tracks merge in any order once the first slice
   works; each is now just "does this model, plugged into the existing
   harness, produce valid envelope messages and results." That held: six
   more models (`annealing`, `bayesian_ab`, `gurobi_routing`, `neural_net`,
   then `ortools_jobshop` and `panel_fit`) were added after the first five
   with no change to the harness. `ortools_jobshop` is the sharpest test of
   that claim, because it did surface a real gap: a CP-SAT solution callback
   fires only on *improvement*, so a cancel went unseen on a hard instance
   for the whole time limit. The fix was two more polls inside the model's
   own callbacks, not a new hook in `job/` — which is what the duck-typed
   contract is supposed to buy. Neither model touched `job/`, `app/` or
   `shared/` at all; what they touched outside their own package is exactly
   the registration list in `models/README.md`, one entry each.
5. `app/client/` starts once step 3 is done, not before. That gate is met and
   the track has started — see `app/client/README.md` for where it is.

## What "done" means per track, before merging

- `models/*`: produces valid envelope messages via `emit()`, respects
  cancellation, writes to its own results table, has unit tests that don't
  require a live Databricks connection (mock the `emit()` callback and the
  results sink).
- `job/`: connects WS with HTTP-push fallback, writes via the Delta
  `write_batch` interface on the size/age/end-of-run flush rule, honours the
  termination signal, degrades cleanly with no app connected at all.
- `app/`: serves SSE with `Last-Event-ID` resume, accepts the job's WS
  connection, exposes `whoami`, reconciles active runs from `run_status` +
  Jobs API on startup, never polls the warehouse for cancel or status.

## A note on what NOT to parallelise prematurely

Don't fan out on `app/client/` pages-per-model until there's at least one
model's real envelope traffic to build the page against — a UI built
against an imagined message shape is exactly the kind of rework this plan is
trying to avoid by freezing `shared/` first.

Worth being straight about how this was actually resolved, since the
per-model views are being fanned out now. The message *shape* is no longer
imagined: `shared/envelope.py` is real, the JSON Schema is generated from it,
and `app/client/src/lib/envelope.contract.test.ts` fails if the TypeScript
drifts from it. Each model's `payload` — the free-form part the envelope
deliberately does not constrain — is hand-derived from that model's own
`emit("progress", ...)` calls, which is why `payloadOf` in
`app/client/src/components/models/contract.ts` returns a `Partial`: a
hand-derived interface can go stale, and some fields are genuinely absent
until a run reaches a given stage. What is still missing is envelope traffic
from a **deployed** run — `databricks bundle deploy` has never been run
against a workspace — so the views are built against locally-run models and
tests, not against production behaviour.
