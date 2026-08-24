# Model expansion + per-job packaging

Two related follow-ups: what the next model should be, and fixing the fact
that every job currently ships the entire repo instead of just what it needs.
Written 2026-08-23 against a repo that had five models; it now has nine, and
the status note below says which of this document's premises that broke.

> **Status note.** Four of this document's premises have been overtaken by
> work that landed after it was written. The packaging *analysis* and
> `scripts/build_model_wheel.py` are unaffected and still correct; the parts
> about models, data and counts are not. Read the body below with these in
> hand:
>
> 1. **"All five existing models deliberately use synthetic, in-memory data …
>    none of them reads a real Unity Catalog table" is no longer true.** Every
>    model now reads `samples.nyctaxi.trips` through `models/_data`, using the
>    job's Spark session, falling back to deterministic synthetic data off
>    workspace. So the gap this doc gives as the main reason for a 6th model —
>    "nothing currently proves a job can cheaply read real UC data via Spark"
>    — is already closed. `nyctaxi_demand` may still be worth building, but on
>    its *telemetry shape* (Spark stage/task progress, a time × zone matrix
>    result) rather than on that justification. It has an agent brief
>    (`.claude/agents/model-nyctaxi-demand.md`) and no code.
> 2. **"Five models" is wrong throughout the body: there are nine.**
>    `gurobi_scheduling`, `gurobi_routing`, `scenario`, `forecasting`, `mcmc`,
>    `bayesian_ab`, `neural_net`, `streaming_results`, `annealing` — all
>    registered in `[tool.dbx-leaning.models]` and all with a job file under
>    `resources/`. Four of them (`annealing`, `bayesian_ab`, `gurobi_routing`,
>    `neural_net`) postdate this document, and `neural_net` is the one that
>    finally makes the per-model dependency split pay for itself: torch
>    reaches exactly one job environment, which `tests/deploy/` asserts.
> 3. **The `samples` inventory here is incomplete.** It was inferred from
>    Databricks docs; `docs/sample-data-inventory.md` was listed from the
>    actual workspace and additionally contains `accuweather`, `bakehouse`,
>    `healthverity`, `tpcds_sf1000` and an `information_schema`. Prefer that
>    file, and prefer `scripts/probe_sample_data.py` over both.
> 4. **`databricks.yml` is no longer "still unbuilt".** It exists, with one
>    job per model, and deploys by workspace-file sync running
>    `entrypoints/run_model.py` — not by wheel. Moving to the per-model wheels
>    this document argues for is therefore a change to a working bundle, and
>    needs one thing this doc does not cover: the generated wheel has no
>    console entry point, so a `python_wheel_task` has nothing to call. Note
>    also that no deploy of any shape has actually been run against a
>    workspace yet.

## New models, using real Databricks sample data

*Superseded — see status note 1. `models/_data` closed this gap; every model
now reads `samples.nyctaxi.trips` on a workspace and falls back to
deterministic synthetic data off one. Kept because the argument for what
makes a model worth adding still holds.*

All five existing models deliberately use synthetic, in-memory data — every
one of their docstrings says so explicitly ("no deep-learning stack for a
platform test," "small on purpose"). None of them reads a real Unity
Catalog table, and none of them uses the job's own Spark session for
anything but the Delta-write fallback. That's a real, currently-untested
seam: a model that legitimately needs Spark/UC as an *input*, not just as
the durable-write path.

Current `samples` catalog contents (inferred from Databricks docs,
2026-08-23 — superseded by `docs/sample-data-inventory.md`, which was listed
from a real workspace): `samples.nyctaxi.trips`, `samples.tpch` (~1TB, TPC-H
benchmark), `samples.tpcds_sf1` (~1GB, TPC-DS benchmark),
`samples.wanderbricks` (a simulated travel-booking platform: users,
properties, bookings, reviews), `samples.databricks` (file-based datasets
under a volume).

### Recommended: a Spark-native aggregation model, `samples.nyctaxi.trips`

Reads real trip data via the job's existing Spark session (no new
dependency — serverless jobs already have one; `job/delta.py`'s Spark
fallback already proves this path works), computes a rolling aggregation
(pickups per hour × pickup zone, say), and reports genuinely different
telemetry: Spark stage/task progress (`SparkContext.statusTracker()` or
listener-based), not an iteration count or a MIP gap. Results are also a
genuinely new shape for this platform — a time × zone matrix, which wants a
heatmap/calendar view, not a line chart or a table. This is the strongest
candidate because it closes an actual platform gap (nothing currently
proves a job can cheaply read real UC data via Spark without touching the
SQL warehouse — the app's "no Spark from the app" rule doesn't apply to the
job, which already has a session) rather than just being another model in
different data clothes.

### Worth a later look, not urgent: Spark MLlib on `samples.wanderbricks`

An ALS-style recommender (property recommendations from booking history)
would exercise *distributed* ML training telemetry — iteration/RMSE
convergence across a Spark job, not a single Python process — genuinely
distinct from forecasting's `SGDRegressor` loop. More setup complexity
(cold-start mapping, whether the booking data is dense enough to be
interesting) than the nyctaxi aggregation model, so treat as something to do
after `nyctaxi_demand` is proven, not part of this round.

### Not recommended without more thought

`tpch`/`tpcds_sf1` are classic OLAP benchmark schemas — good for proving
raw query performance, but don't obviously map to an interesting *model*
with its own telemetry shape; more likely to become "another aggregation"
than a genuinely new class. Skip unless a specific use case for them shows
up.

## Per-job packaging: stop shipping the whole repo to every job

**The gap, precisely:** `pyproject.toml`'s per-model *dependency* scoping
already exists and is correct — the `gurobi`, `forecasting`, `mcmc`,
`bayesian`, `nn`, `scenario`, `streaming` and (deliberately empty)
`annealing` extras each pull in only that model's own libraries.
What's missing is *source* scoping: `[tool.setuptools] packages = ["shared",
"app", "job", "models"]` means any wheel built from this repo bundles the
entire `models/` package — every model's source code — regardless of which
one a given job actually runs. `job/loader.py` only *imports* the one model
named by `DBX_MODEL` at runtime, but the sibling models' source is still
physically present in the deployed artifact.

**Verified fix, not assumed:** Databricks Asset Bundles support per-task
libraries natively — each task in a job can reference its own wheel,
independent of sibling tasks in the same job or bundle
(`libraries: [whl: ./path/to/wheel.whl]` per `task_key`). Databricks' own
guidance for a monorepo is explicit: *"build task-specific wheels
separately and reference them by path, rather than relying on automatic
bundling of your entire source tree."* That's exactly this situation.

**Implemented and verified**, not just proposed: `scripts/build_model_wheel.py`
(new, in this delivery). It stages `shared/` + `job/` + exactly one
`models/<name>/` into a throwaway directory with a generated
`pyproject.toml` — dependencies are the existing core deps plus that
model's existing extra, reused as-is, nothing about dependency scoping
changes — and builds a wheel from there with `uv build`. Test-built against
the actual repo for every registered model (`uv run python
scripts/build_model_wheel.py --all`): every wheel contains exactly
`shared/`, `job/` (including `job/drivers/`), and its one `models/<name>/`
— confirmed by inspecting each wheel's file listing — with dependency
metadata scoped to that model's own extra. An unregistered model name fails
loudly rather than silently shipping with no dependencies — confirmed with a
throwaway `models/fake_model/`.

It also excludes `deltalake`/`pyarrow` by default. That is not an oversight:
`job/delta.py`'s `DeltaRsWriter` raises `NotImplementedError`, so Spark is
the write path and those two would otherwise ship to every job to satisfy an
import that never runs. `--with-delta` exists for the change that makes
delta-rs real.

One naming gotcha worth knowing about going in: the `models/` directory
name and the `pyproject.toml` extra name don't always match
(`models/gurobi_scheduling` ↔ extra `gurobi`; `models/streaming_results` ↔
extra `streaming`; `models/neural_net` ↔ extra `nn`), and two models share
one extra (`gurobi_scheduling` and `gurobi_routing`, deliberately — one
gurobipy pin, one bundled-licence expiry, two jobs).

**Where that map lives has since moved, and this is the correction most
likely to trip someone grepping.** This document described a
`MODEL_PACKAGE_TO_EXTRA` dict inside `scripts/build_model_wheel.py`. There is
no such name in the repo any more. `scripts/export_requirements.py` was
keeping its own second copy of the same fact, and a model registered in one
and not the other deploys with the wrong dependencies rather than failing —
so the map was consolidated into **`[tool.dbx-leaning.models]` in
`pyproject.toml`**, next to the extras it points at, and both scripts read it
through `scripts/_registry.py` (`model_extras()`, `extra_for()`,
`model_names()`, and `discovered_packages()` for what is actually on disk).
An unregistered model raises `UnregisteredModel` naming the models that *are*
registered. `tests/deploy/test_bundle.py::test_every_model_on_disk_is_in_the_registry`
asserts the registry and `models/` cannot drift apart. Adding a model is
still a one-line entry; it is just a one-line entry in `pyproject.toml`.

**Proposed shape for `databricks.yml`:** one job per model (matching
`DBX_JOB_IDS`'s existing "model name → job id" shape), each task's
`libraries` pointing at the wheel `scripts/build_model_wheel.py` produced
for that model — built as a deploy step before the bundle deploy, output
into `dist/<model>/`. The `app/` deployment is unaffected — it already only
needs `shared/` + `app/` + the `app` extra, and never imports anything from
`models/`.

**This is no longer greenfield advice.** The bundle exists (`databricks.yml`
plus one `resources/model_<name>.job.yml` per model), and it took the *other*
route: code reaches the workspace by file sync, each job runs
`entrypoints/run_model.py` from the synced tree with a `spark_python_task`,
and per-model *dependencies* come from `deploy/requirements/<name>.txt`
exported from `uv.lock`. So dependency scoping is solved and source scoping
is not — every job's synced tree still contains every model's source. That
is a smaller problem than it was when this was written (a synced tree costs
workspace storage, not install time, and the per-model requirements already
stop torch reaching the other eight jobs), which is why it has not been
urgent.

Switching to wheels is therefore a change to a working bundle rather than a
choice made from scratch, and it needs one thing this document does not
cover: the generated wheel has **no console entry point**, so a
`python_wheel_task` has nothing to call. Either the wheel gains a
`[project.scripts]` entry wrapping `job.main`, or the task stays a
`spark_python_task` and the wheel is attached as a library alongside it.
Decide that before touching `resources/`.

## Sources

- [Databricks sample datasets](https://docs.databricks.com/aws/en/discover/databricks-datasets)
- [Databricks Asset Bundles library dependencies](https://learn.microsoft.com/en-us/azure/databricks/dev-tools/bundles/library-dependencies)
