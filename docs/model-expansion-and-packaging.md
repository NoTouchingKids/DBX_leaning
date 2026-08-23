# Model expansion + per-job packaging

Two related follow-ups: what a 6th (and maybe 7th) model should be, and
fixing the fact that every job currently ships the entire repo instead of
just what it needs. Written 2026-08-23, grounded in the real repo and
verified current platform facts, not assumption.

> **Status note, added on merge.** Three of this document's premises were
> overtaken by work that landed between it being written and it being merged.
> The packaging analysis and `scripts/build_model_wheel.py` are unaffected and
> still correct; the parts about models and data are not:
>
> 1. **"All five existing models deliberately use synthetic, in-memory data …
>    none of them reads a real Unity Catalog table" is no longer true.** All
>    five now read `samples.nyctaxi.trips` through `models/_data`, using the
>    job's Spark session, falling back to deterministic synthetic data off
>    workspace. So the gap this doc gives as the main reason for a 6th model —
>    "nothing currently proves a job can cheaply read real UC data via Spark"
>    — is already closed. `nyctaxi_demand` may still be worth building, but on
>    its *telemetry shape* (Spark stage/task progress, a time × zone matrix
>    result) rather than on that justification.
> 2. **The `samples` inventory here is incomplete.** It was inferred from
>    Databricks docs; `docs/sample-data-inventory.md` was listed from the
>    actual workspace and additionally contains `accuweather`, `bakehouse`,
>    `healthverity`, `tpcds_sf1000` and an `information_schema`. Prefer that
>    file, and prefer `scripts/probe_sample_data.py` over both.
> 3. **`databricks.yml` is no longer "still unbuilt".** It exists, with one
>    job per model, and currently deploys by workspace-file sync running
>    `entrypoints/run_model.py` — not by wheel. Moving to the per-model wheels
>    this document argues for is therefore a change to a working bundle, and
>    needs one thing this doc does not cover: the generated wheel has no
>    console entry point, so a `python_wheel_task` has nothing to call. See
>    the merge commit for the specifics.

## New models, using real Databricks sample data

All five existing models deliberately use synthetic, in-memory data — every
one of their docstrings says so explicitly ("no deep-learning stack for a
platform test," "small on purpose"). None of them reads a real Unity
Catalog table, and none of them uses the job's own Spark session for
anything but the Delta-write fallback. That's a real, currently-untested
seam: a model that legitimately needs Spark/UC as an *input*, not just as
the durable-write path.

Current `samples` catalog contents (verified against Databricks docs,
2026-08-23): `samples.nyctaxi.trips`, `samples.tpch` (~1TB, TPC-H benchmark),
`samples.tpcds_sf1` (~1GB, TPC-DS benchmark), `samples.wanderbricks` (a
simulated travel-booking platform: users, properties, bookings, reviews),
`samples.databricks` (file-based datasets under a volume).

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
interesting) than the nyctaxi aggregation model, so treat as a possible 7th
once the 6th is proven, not part of this round.

### Not recommended without more thought

`tpch`/`tpcds_sf1` are classic OLAP benchmark schemas — good for proving
raw query performance, but don't obviously map to an interesting *model*
with its own telemetry shape; more likely to become "another aggregation"
than a genuinely new class. Skip unless a specific use case for them shows
up.

## Per-job packaging: stop shipping the whole repo to every job

**The gap, precisely:** `pyproject.toml`'s per-model *dependency* scoping
already exists and is correct — the `gurobi`/`forecasting`/`mcmc`/
`scenario`/`streaming` extras each pull in only that model's own libraries.
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
the actual repo for all five models (`uv run python
scripts/build_model_wheel.py --all`): every wheel contains exactly
`shared/`, `job/` (including `job/drivers/`), and its one `models/<name>/`
— confirmed by inspecting each wheel's file listing — with dependency
metadata scoped to that model's own extra. An unregistered model name (no
entry in the script's `MODEL_PACKAGE_TO_EXTRA` map) fails loudly rather
than silently shipping with no dependencies — confirmed with a throwaway
`models/fake_model/`.

One naming gotcha worth knowing about going in: the `models/` directory
name and the `pyproject.toml` extra name don't always match
(`models/gurobi_scheduling` ↔ extra `gurobi`; `models/streaming_results` ↔
extra `streaming`). The script's `MODEL_PACKAGE_TO_EXTRA` map is explicit
about this rather than guessing from the name — a new model gets a one-line
entry there, and the script errors clearly if it's missing.

**Proposed shape for `databricks.yml`, once it exists:** one job per model
(matching `DBX_JOB_IDS`'s existing "model name → job id" shape), each task's
`libraries` pointing at the wheel `scripts/build_model_wheel.py` produced
for that model — built as a deploy step before the bundle deploy, output
into `dist/<model>/`. The `app/` deployment is unaffected — it already only
needs `shared/` + `app/` + the `app` extra, and never imports anything from
`models/`.

This sits exactly where the repo's own README already flags the next piece
of work ("no `app.yaml`, no `databricks.yml` bundle, no secrets wiring").
Worth building the bundle against this scoped-wheel shape from the start,
not the current one-wheel-has-everything shape — reworking a bundle after
the fact costs more than starting from the right shape.

## Sources

- [Databricks sample datasets](https://docs.databricks.com/aws/en/discover/databricks-datasets)
- [Databricks Asset Bundles library dependencies](https://learn.microsoft.com/en-us/azure/databricks/dev-tools/bundles/library-dependencies)
