# Datasets worth training on, under Free Edition's constraints

Written 2026-08-24, after the `samples`-only restriction was lifted. The
question this answers: *given that a model cannot reach the internet at run
time, what is actually available to train on, and which of it is good?*

Everything marked **verified** below was checked, not recalled. Everything
else is labelled as unchecked, because this repo has already paid for the
difference twice.

## The constraint that decides the shape of the answer

Outbound traffic is restricted to trusted domains, **from job compute as well
as from the app**. A model cannot download a dataset when it runs. This is
the same restriction that rules out Gurobi's WLS licence, and it fails in the
worst available way: the code works on a laptop and then hangs or errors on
the job, after the run has started and taken one of five account-wide task
slots.

So there are four egress-free routes, with very different costs:

| Route | Cost | Good for |
|---|---|---|
| **A Unity Catalog table** | None. The loader already does this | Anything already in `samples`, or landed there once |
| **Data bundled in a PyPI wheel** | Inflates every model environment that lists the extra | Small, well-understood benchmark sets |
| **A Unity Catalog volume** — `/Volumes/samples/databricks/datasets/` | None. Governed, and read like any volume path | File-based sample data. Invisible to `information_schema.tables`, which is why earlier listings missed it |
| **Landed in UC out of band** — volume upload, Marketplace, Delta Sharing | One-off human step, then free forever | Anything else |

`pip install` runs before the model does, which is why route two works at all:
by run time the data is already on disk. That only holds for packages that
**bundle** their data. A package that downloads on first use — `fetch_*` in
scikit-learn, `torchvision.datasets`, HuggingFace `datasets`,
`statsmodels.api.datasets.get_rdataset` — is route one's problem wearing
route two's clothes, and will fail on the job.

## Route 1: what is already in `samples`

Table names verified 2026-08-23 from `information_schema.tables`. That
listing showed nine schemas — `nyctaxi`, `bakehouse`, `accuweather`,
`wanderbricks`, `tpcds_sf1`, `tpcds_sf1000`, `tpch`, `healthverity`,
`information_schema`. The Databricks docs page for this catalog names only
five (`databricks`, `nyctaxi`, `tpcds_sf1`, `tpch`, `wanderbricks`), so **the
workspace has more than the docs describe, and less overlap than expected in
both directions** — the docs list a `databricks` schema the table query
missed, because it holds a volume rather than tables. Trust the workspace,
and check both views. **Column
names are confirmed only for the seven tables listed in
`sample-data-inventory.md`** — `information_schema.columns` returns nothing
for most of this catalog, so treat any column named below as unchecked unless
that file says otherwise. Row counts are unknown throughout: nothing in the
metadata views carries them, and nobody has run a `COUNT(*)`.

### `accuweather` — the strongest ML data in the catalog

Twelve tables, and the reason they matter is the pairing: there are
`historical_*` **and** `forecast_*` tables at the same grains
(`hourly`, `daily_calendar`, `daynight`), each in `metric` and `imperial`.

```
historical_hourly_{metric,imperial}      forecast_hourly_{metric,imperial}
historical_daily_calendar_{...}          forecast_daily_calendar_{...}
historical_daynight_{...}                forecast_daynight_{...}
```

That gives something genuinely rare in a sample catalog: **a forecast to
score against**. A model can be evaluated not just on held-out actuals but
against a commercial vendor's own forecast for the same timestamp — a real
baseline rather than a naive-persistence one. For `forecasting` and
`streaming_results` this is a considerably better story than a taxi demand
curve.

`historical_hourly_imperial` is one of the seven tables with **verified**
columns — 49 of them, including `date TIMESTAMP`, `city_name`, `latitude` /
`longitude`, and a wide numeric block (`temperature`, `temperature_dew_point`,
`temperature_realfeel`, `humidity_relative`, `pressure`, `pressure_msl`,
`wind_speed`, `wind_direction`, `wind_gust`, `visibility`,
`solar_irradiance`, `solar_radiation_net`, `cloud_cover_total`,
`precipitation_lwe`, `snow_lwe`, `index_uv`).

Two traps in it, both visible in the column types:

- **Several `cloud_cover_*`, `rain_*`, `ice_*` and `snow*` columns are
  `STRING`, not numeric**, while their neighbours are `DOUBLE`. Casting
  blindly across "all the weather columns" will produce nulls or throw.
- **Every column is nullable.** `AVG()` over a sparse hour returns NULL, which
  is exactly the defect this repo already hit once (`float(None)`). Use
  `Dataset.floats(..., default=)` or `dropna`.

### `tpcds_sf1` and `tpcds_sf1000` — 24 tables each

A full retail star schema: `store_sales`, `catalog_sales`, `web_sales`,
`store_returns`, `inventory`, `promotion`, `customer`,
`customer_demographics`, `household_demographics`, `income_band`, `item`,
`date_dim`, `time_dim`, and more.

The two scale factors are the point: `sf1` is small enough to iterate on and
`sf1000` is large enough that the 2X-Small warehouse and a serverless job
will both feel it. `store_sales` joined to `date_dim` is the standard
seasonal demand series, and `customer_demographics` + `income_band` give a
tabular classification target that is not derived from its own features —
unlike the taxi table, where fare and distance are near-deterministic in each
other because a meter is a formula.

### `wanderbricks` — 16 tables, and the widest ML surface

`bookings`, `booking_updates`, `payments`, `clickstream`, `page_views`,
`properties`, `property_amenities`, `property_images`, `amenities`,
`reviews`, `users`, `hosts`, `employees`, `destinations`, `countries`,
`customer_support_logs`.

This is a travel-booking business end to end, which means it carries the
targets a sample catalog usually lacks: conversion (clickstream → bookings),
cancellation (`booking_updates`), review score, support-ticket outcome. It is
also the only schema here with an obvious **two-arm split for `bayesian_ab`**
— by destination, property type or channel — instead of the contrived
weekend-versus-weekday comparison it runs on today.

`destinations` is the outstanding candidate for `gurobi_routing`'s
coordinates, and remains unchecked: `information_schema.columns` returns
nothing for this schema. `bakehouse.sales_franchises` is the *verified*
coordinate source (see `sample-data-inventory.md`), so routing does not have
to wait on this.

### `healthverity.claims_sample_synthetic`

Synthetic health claims. Unchecked columns. Worth a look for a classification
or cost-regression target, with the caveat that "synthetic" means the signal
in it is whatever the generator put there.

### `bakehouse` — six tables, all columns verified

The only schema where every column is confirmed. Small, clean, and the best
fit for `gurobi_scheduling` (transactions per franchise per hour is a staffing
demand curve, which taxi trips are not). See `sample-data-inventory.md` for
the full column listing and the `LONG`-money trap.

**It is also the one entry on this page that has stopped being a
proposal.**
`job/models/ortools_jobshop` reads `sales_transactions` today: a job is one
franchise's sales of one product on one day, and `SUM(quantity)` is what
drives every duration in the instance. That it was written against a verified
column listing rather than a guess is the whole reason it could be, and it is
worth knowing what the verification bought — the model deliberately touches
neither `unitPrice` nor `totalPrice`, because both are `LONG` and nothing
confirms whether that is minor units or a rounded figure. Verified columns do
not make every column usable; they make it possible to say which ones are
not.

### `nyctaxi.trips`

What nine of the eleven models read today, through the two loaders in
`job/models/_data/datasets.py`. One table. Its columns are **not** in
`information_schema.columns` — absence there means nothing, see the inventory
— and the loaders' three columns have never been confirmed against it. The
two exceptions are `ortools_jobshop` (`bakehouse`, above) and `panel_fit`,
whose table does not exist at all — see the last two rows of the per-model
table below.

## Route 2: datasets bundled in a wheel

**Verified egress-free.** Each of these was loaded in a process with
`socket.socket` replaced by a raising stub, so a silent download would have
failed rather than passed.

### scikit-learn — already a dependency of `forecasting`

`sklearn.datasets.load_*` ship inside the wheel as CSVs:

| Loader | Shape | Target |
|---|---|---|
| `load_digits` | 1797 x 64 | 10 classes |
| `load_breast_cancer` | 569 x 30 | 2 classes |
| `load_diabetes` | 442 x 10 | regression |
| `load_wine` | 178 x 13 | 3 classes |
| `load_iris` | 150 x 4 | 3 classes |
| `load_linnerud` | 20 x 3 | 3 regression targets |

`load_digits` is the one worth noting for **`neural_net`**: 64 features, ten
balanced classes, and a genuine signal, against the current model's three
near-collinear taxi columns which forced `EXCLUDED_COLUMNS` to withhold the
leaky ones. It is also small enough to stay a *small* torch classifier.

**`fetch_*` is a different thing entirely** — `fetch_california_housing`,
`fetch_covtype`, `fetch_openml`, `fetch_20newsgroups` all download on first
call and will fail on the job. The naming similarity is a trap worth a
comment wherever one is used.

### statsmodels — not currently a dependency

Its datasets are bundled and load with the network blocked. Verified:

| Dataset | Shape | Why |
|---|---|---|
| `co2` | 2284 x 1 | Weekly CO2. Strong trend **and** annual seasonality, long enough for a real rolling-origin backtest. The best forecasting series available without touching UC |
| `macrodata` | 203 x 14 | Quarterly US macro. Multivariate regression with genuinely correlated series |
| `sunspots` | 309 x 2 | Yearly, strongly cyclical, famously hard |
| `grunfeld` | 220 x 5 | Panel data — firm x year |
| `nile`, `elnino`, `engel`, `stackloss`, `heart` | 21–100 rows | Too small for this platform; listed so nobody re-checks them |

`get_rdataset` downloads and must not be used.

Adding statsmodels costs a new extra on whichever models use it. Weigh that
against `accuweather`, which needs no dependency at all — the honest reason to
prefer `co2` is length and cleanliness, not availability.

## Route 3: `/Volumes/samples/databricks/datasets/` — the UC-native file source

`samples` contains a `databricks` schema holding a **volume** called
`datasets`: "file-based sample datasets for building data pipelines"
(Databricks docs, *Sample datasets*, retrieved 2026-08-24).

This is the file-based source to build on. It is inside Unity Catalog, so it
is governed, egress-free and read exactly like any other volume path:

```python
dbutils.fs.ls("/Volumes/samples/databricks/datasets/")     # what's there
spark.read.csv(path, header=True, inferSchema=True)        # read one
```

```sql
LIST '/Volumes/samples/databricks/datasets/'
```

**It does not appear in `information_schema.tables`, because a volume is not
a table.** That is why the 2026-08-23 listing showed no `databricks` schema in
`samples` at all — it was there the whole time, invisible to a table query.
Anything that inventories this catalog by tables alone will keep missing it,
which is worth remembering the next time a listing looks complete.

Its contents are unlisted here for the same reason the third-party table below
is: nobody has run the probe yet. `scripts/probe_sample_data.py` lists it.

### The legacy DBFS mount, and why not to use it

`/databricks-datasets` is the older mount. The same docs page is unambiguous
about it:

> Databricks recommends against using DBFS and mounted cloud object storage
> for most use cases in Unity Catalog-enabled Databricks workspaces.

and

> The availability and location of Databricks datasets are subject to change
> without notice.

That second sentence is the disqualifying one. A model pinned to a path with
no stability guarantee breaks on someone else's schedule, in a job, after
claiming one of five account-wide slots. The probe still lists the mount —
knowing whether it is reachable is worth something — but a loader should read
the volume.

*(An earlier version of this file called this mount "the biggest maybe" and
the richest option available. That was written before the docs page was read
and was wrong twice over: the mount is deprecated in UC workspaces, and the
third-party CSV datasets it was assumed to contain are not in it at all — see
below.)*

## Route 4: the third-party CSV datasets are an upload, not a mount

The "third-party sample datasets in CSV format" the Databricks docs name are
**not** shipped anywhere in the workspace. The documented workflow is:
download the CSV to your own machine, upload it into the workspace, then
query it. They are Route 5 material — external data landed by hand — and the
page's value is as a curated list of sources known to work in CSV form:

| Source | Notes |
|---|---|
| [OWID Dataset Collection](https://github.com/owid/owid-datasets) | Our World in Data, on GitHub. Wide, clean, long time series across many domains — the strongest ML candidate on the list, and the one with a model already waiting for it: `job/models/panel_fit` defaults to `main.dbx_leaning.owid_country_year`, a name nobody has landed |
| [Data.gov CSV datasets](https://catalog.data.gov/dataset/?res_format=CSV) | Very large catalogue, quality varies a lot by publisher |
| [The Squirrel Census](https://www.thesquirrelcensus.com/data) | Small and charming; a classification toy, not a training set |
| Kaggle: Diamonds | ~54k rows, classic tabular regression. Needs a Kaggle account |
| Kaggle: NYC Taxi Trip Duration | Competition data. Needs a Kaggle account |

Each still has to reach Unity Catalog before a model can read it, because a
job cannot download any of them at run time.

## Route 5: bringing something else in

Covered in `free-edition-constraints.md`, "Getting data in from outside".
Short version: a UC volume upload is the route that cannot go wrong;
Databricks Marketplace is the tidiest **if** it is reachable from Free Edition,
which is **unverified** — the constraints doc records that account-level APIs
and the account console are not available, and Marketplace may sit behind
them.

## What this would actually change, per model

Nothing here is a decision. Each is one loader in `job/models/_data/datasets.py`
plus a config field, and the synthetic fallback stays mandatory regardless.

| Model | Today | Best available upgrade | Why it is better |
|---|---|---|---|
| `forecasting` | taxi hourly | `accuweather.historical_hourly_*`, scored against `forecast_hourly_*` | A vendor forecast as the baseline, instead of held-out actuals alone |
| `streaming_results` | taxi hourly | same, or statsmodels `co2` | A rolling-origin backtest wants length above everything |
| `neural_net` | taxi trips, 3 near-collinear columns | `load_digits`, or `tpcds_sf1` demographics | Real signal, and no need to withhold leaky features |
| `bayesian_ab` | weekend vs weekday fare | `wanderbricks.reviews` / `bookings` | A real two-arm split rather than one contrived from a table with no A/B in it |
| `gurobi_scheduling` | taxi hourly demand | `bakehouse.sales_transactions` + `sales_franchises` | Staffing shifts against transaction volume is what the MILP is for. **Columns verified** |
| `gurobi_routing` | stops derived from distance | `bakehouse.sales_franchises` coordinates | Real geometry. **Columns verified** — this is the cheapest real upgrade on the list |
| `scenario` | taxi baseline | `wanderbricks.bookings` | Booking demand has natural scenario levers |
| `mcmc` | fare ~ distance | fine as-is | A real regression with a plausible slope |
| `annealing` | taxi trips as knapsack items | fine as-is | The point of this model is the empty dependency extra, not the data |
| `ortools_jobshop` | `bakehouse.sales_transactions` — **already** | fine as-is | The only model on this platform reading real data that is not taxi data. Grouping, job size and product mix are read; the stage recipes and every rate are invented and labelled as such in `instance.py` |
| `panel_fit` | its own synthetic panel, always | an OWID CSV landed in UC under any name, then `{"table": …}` in the model config | The one row here whose upgrade is a *human* step rather than a loader. `DEFAULT_PANEL_TABLE` names a table that does not exist, so every default run falls back and says so in its provenance |

The three `bakehouse` rows are the only ones whose columns are confirmed.
Every other row needs a `DESCRIBE` first — which is what
`scripts/probe_sample_data.py` now does for tables
`information_schema.columns` skips. The `panel_fit` row is a different kind
of unknown: not a table whose columns are unverified, but a table that is
not there, which the model already handles by degrading rather than failing.
