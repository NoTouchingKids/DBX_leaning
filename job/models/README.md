# Writing a model

A model is a plain Python object. No base class, no registration, no import
from the platform — the harness discovers a small set of conventional names
on whatever object your package hands back. See `docs/architecture.md`
("Why models are duck-typed") for why it is this way rather than a
`ModelAdapter` hierarchy.

The authoritative list of names is `CONVENTIONS` in `job/loader.py`. This
file is the same contract in prose. If the two disagree, the code is right
and this file is stale — say so.

## The whole contract

```python
# job/models/my_model/__init__.py
from .model import MyModel


def build_model(config: dict) -> "MyModel":  # module-level factory
    return MyModel(config)
```

```python
class MyModel:
    results_table = "results_my_model"  # optional; DBX_RESULTS_TABLE overrides
    preview_axes = ("t", "value")  # optional; enables LTTB previews

    def __init__(self, config: dict | None = None):
        self.emit = None  # set by the harness before build/run
        self.should_cancel = None  # set by the harness; call it, don't inspect it

    def build(self): ...  # optional
    def run(self): ...  # required (unless you expose a gurobipy model)
    def results(self) -> list[dict]: ...  # optional
```

| What the harness looks for | Accepted names (first match wins) | Required |
|---|---|---|
| Factory (module level) | `build_model`, `create_model`, `make_model`, `Model` | yes |
| Wiring hook | `attach(emit, should_cancel)` | no — falls back to setting the two attributes |
| Build step | `build`, `setup`, `prepare` | no |
| The blocking call | `run`, `solve`, `fit`, `sample`, `execute` | yes\* |
| Results accessor | `results`, `get_results`, `result_rows` | no |
| Gurobi model | `grb_model`, `gurobi_model` | \* instead of a blocking call |
| Your Gurobi callback | `gurobi_callback`, `callback` | no |
| Results table | `results_table` | no |
| Preview axes | `preview_axes` — `(x, y)` column names | no |

\* Expose **either** a blocking call **or** a `gurobipy.Model`. A Gurobi model
must not call `optimize()` itself: the harness owns the solve so it can attach
its own progress/log/cancellation observers to the single callback slot Gurobi
allows, composed with yours. Calling `optimize()` yourself bypasses
cancellation and progress entirely.

If nothing runnable is found, the loader fails with a message listing every
name it tried. You should never need to read `job/loader.py` to find out what
it wanted.

## `emit(type, **fields)`

Your entire coupling surface. Four types, documented in full in
`docs/message-envelope-spec.md`:

```python
self.emit("log", message="built 840 vars", source="model", phase="build")
self.emit(
    "progress",
    elapsed_seconds=12.5,
    percent_complete=40.0,
    primary_metric=0.03,
    primary_metric_label="mip_gap",
    payload={"nodes_explored": 900},
)
self.emit("result", rows=[{"t": 0, "value": 1.2}, ...])
```

- You never supply `run_id`, `seq` or `ts` — the harness stamps all three.
- **`emit` never raises for a transport problem.** Nobody listening is a
  normal state of affairs. It *does* raise if your message is malformed,
  because that is your bug and swallowing it is how message shapes drift.
- `emit` is safe to call from whatever thread your blocking call runs on.
- Emitting `status` is the harness's job, not yours. Return a status string
  from your `run()` if you need to say something it could not infer
  (`"INFEASIBLE"`). It has to be a real `RunStatus` member — anything else
  you return is treated as a *detail* string on a `SUCCEEDED` run, not as a
  status, so a typo degrades quietly rather than failing
  (`job/drivers/self_driving.py`). A cancelled run overrides whatever you
  returned: cancellation is decided by the harness, not by you.

### Results

Pass `rows=[...]` to `emit("result", ...)` and the harness will:

1. write them to your results table (stamped with `run_id` and `chunk_index`),
2. count them into `row_count` — the field that distinguishes "succeeded,
   wrote 8,760 rows" from "succeeded, wrote nothing",
3. build a bounded `preview` (LTTB if you set `preview_axes`),
4. fill in `fetch_hint` so a client can pull the full set on demand.

Do not pass `row_count` yourself; do not put the rows on the message. Call it
**once per chunk** if your model produces results incrementally — each chunk
gets its own `chunk_index`, its own count, and `final=False` until the last.

If you never call it, the harness calls your `results()` accessor once at the
end instead. It will not do both.

## Getting data

Free Edition ships Databricks' `samples` catalog, and `job/models/_data` reads it
— falling back to a deterministic generator when there is no workspace, so
your model and its tests run anywhere.

**`samples` is no longer the only permitted source.** External data is
allowed where it genuinely fits the model. Two things to know before reaching
for it:

- `job/models/_data.load()` was never samples-specific. It takes arbitrary SQL
  and a `source` label, so any Unity Catalog table works today with no change
  to this module. Only the two loaders in `datasets.py` hardcode
  `samples.nyctaxi.trips`; a new loader is the whole job, and two models have
  since written one — see the loader paragraph below for where they put it.
- **A model cannot fetch data over the internet at run time.** Free Edition
  restricts outbound traffic to trusted domains, and that restriction has not
  lifted — it is the same one that rules out Gurobi's WLS licence. "External
  data" therefore means *get it into Unity Catalog first*: upload to a
  volume, add a Databricks Marketplace data product, or land it once through
  a notebook. A model that calls out to an API at run time will work on your
  laptop and hang or fail on the job. See `docs/free-edition-constraints.md`.

Whatever the source, the three rules below still apply, and the fallback is
still what keeps a model runnable offline. It can also be the *only* path a
model ever takes: `panel_fit`'s `DEFAULT_PANEL_TABLE` deliberately names
`main.dbx_leaning.owid_country_year`, which nobody has landed, so every run
at the default configuration falls back to its generator and says so in its
provenance. Naming the table the model actually wants, and reporting the
fallback loudly, beats pointing at something that exists but is the wrong
shape — but it does mean a generator built to be worth fitting rather than
merely present.

There are two loaders in `job/models/_data/datasets.py` today, both over
`samples.nyctaxi.trips`, and both return a `Dataset`:

```python
from job.models._data import epoch_ms, nyc_taxi_hourly, nyc_taxi_trips

data = nyc_taxi_hourly(days=60)  # hour_ts (epoch ms), trips, avg_fare, avg_distance
data = nyc_taxi_trips(limit=2000)  # trip_distance, fare_amount, duration_min

data.rows  # list[dict]
data.column("trips")  # one column, as-is
data.floats("trips")  # raises on a NULL; pass default= to substitute
data.dropna("trips")  # or drop whole rows, keeping columns aligned
data.synthetic  # did it fall back?
data.provenance  # a line for a log message
data.describe()  # data_source / data_synthetic / data_rows / data_fallback_reason

epoch_ms(value)  # datetime | int | float | date | ISO string -> epoch ms
```

`nyc_taxi_hourly` picks the demand-curve shape (forecasting, scheduling,
scenario); `nyc_taxi_trips` picks the row-per-observation shape
(mcmc, neural_net, annealing, routing). `bayesian_ab` uses both, one per
comparison. Nothing stops a new model adding a third loader to
`job/models/_data/datasets.py` — one function per *dataset*, not per model, so
two models asking the same question get the same shape.

Two models read neither loader, and where they put their own is the pattern
to copy. `ortools_jobshop` builds its shop floor from
`samples.bakehouse.sales_transactions` (`instance.py`), and `panel_fit` fits
a country x year panel (`panel_data.py`); both call `models._data.load()`
directly with their own SQL. They live in their own package because
`datasets.py`'s rule is one function per *dataset* and each of those datasets
has exactly one consumer today. The moment a second one appears, the loader
moves next to the taxi pair unchanged — it already goes through `load()`, so
there is nothing to rewrite.

Three rules that come out of this, learned the hard way:

- **Load in `build()`, not `__init__`.** The harness wires `emit` *after*
  constructing your model, so a provenance log emitted from `__init__` goes
  nowhere.
- **Put `data.describe()` on your result rows**, not only in a log. Logs are
  droppable by contract; the question "was this real data?" has to survive to
  the durable record.
- **Never assume a column is non-null.** A real `AVG()` over an empty hour
  returns NULL, and that only ever shows up on a workspace.
- **Never assume a timestamp column's Python type.** Spark hands back a
  `datetime` for a TimestampType; the synthetic fallback returns an `int`. A
  model doing `int(row["hour_ts"])` works offline and raises on a workspace.
  `epoch_ms()` exists so you do not have to care which you got.

Run `scripts/probe_sample_data.py` on a workspace to see what is actually
there. "Falls back cleanly" and "is reading real data" are different states,
and only a workspace can tell you which one you are in.

## Cancellation

```python
for i, item in enumerate(work):
    if self.should_cancel():
        break  # keep what you have; do not raise
    ...
```

A cancelled run is a **clean outcome**, not an error. Results you already
produced are written and the run reports `CANCELLED`. Check the signal at
whatever granularity is natural for your model — between scenarios, between
epochs, between draws — not mid-computation.

## Non-goals for every model in this repo

- No WebSocket, HTTP, Delta or SQL code. Only `emit(...)`.
- No knowledge of `run_id`, `seq` or timestamps.
- No import from `job/`, `app/` or `shared/`. You conform to a documented
  contract; you do not call into the platform. A model must behave identically
  run standalone, which is also how its tests run.

## Registering one, so it actually deploys

Writing the package is most of the work but not all of it. A model is a
microservice here — its own job, its own serverless environment, its own
dependency list — and five things outside `job/models/<name>/` have to know it
exists. `/new-model` scaffolds the package; it does not currently do this
list, so work through it by hand.

1. **`[tool.dbx-leaning.models]` in `pyproject.toml`** — one line mapping
   `<name>` to the `[project.optional-dependencies]` extra carrying its
   libraries. This is the single registry; `scripts/export_requirements.py`
   and `scripts/build_model_wheel.py` both read it through
   `scripts/_registry.py`, so there is nowhere else to also declare it.
   Directory name and extra name do not have to match, and two models may
   share one extra (`gurobi_scheduling` and `gurobi_routing` do). An extra
   may be **empty** — `job/models/annealing` maps to one deliberately, to prove
   the split can produce a minimal environment.
2. **The extra itself**, if it is new. `uv add --optional <extra> <package>`,
   which rewrites `pyproject.toml` *and* `uv.lock` — do that as its own
   sequential commit if other tracks are running in parallel.
3. **`deploy/requirements/<name>.txt`** — generated, never hand-written:
   `uv run python scripts/export_requirements.py`.
4. **`resources/model_<name>.job.yml`** — copy the closest existing one and
   change it. The duplication is intentional; these files are meant to
   diverge.
5. **`resources/app.yml`** — the new job's id has to appear in `DBX_JOB_IDS`,
   or the app has no way to trigger it.

`tests/deploy/` enforces every one of those and names what is missing, so run
`uv run pytest tests/deploy` before you believe you are finished.

**One thing nothing enforces:** your `results_table` has to exist in
`uc_ddl/002_model_results.sql`. No test cross-checks the two, so a model can
pass the entire suite and then fail its first real write on a workspace with
a table-not-found. Add the `CREATE TABLE IF NOT EXISTS` when you add the
model, and include the four provenance columns (`data_source`,
`data_synthetic`, `data_rows`, `data_fallback_reason`) if you put
`data.describe()` on your rows — which you should.

## Testing

Mock the two things the harness hands you — that is the entire test harness:

```python
model = build_model({"n": 10})
messages = []
model.emit = lambda type, **f: messages.append((type, f))
model.should_cancel = lambda: False
model.run()
```

`job.loader.describe_object(model)` tells you what the harness would discover,
so a model's own test suite can assert its surface without importing by string.
