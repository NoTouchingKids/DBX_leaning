# What's actually in the `samples` catalog

Verified against a real Free Edition workspace on **2026-08-23** by listing
`samples.information_schema.tables`. This file exists because guessing at this
already cost us a defect: `hour_ts` was documented as epoch milliseconds on the
strength of inference, while Spark actually returns a `datetime` — invisible in
every test, and a hard failure on the first real run.

**Treat this as fact and the column-level details as still unverified.** Table
names below are confirmed. Columns are not: run `scripts/probe_sample_data.py`
on a workspace to get them from `information_schema.columns`, and write
loaders against that output rather than against a plausible-sounding name.

`scripts/probe_sample_data.py` **has never been run against a real
workspace.** It is written and it is the right tool; nobody has executed it
yet. So every column name in the two loaders in `models/_data/datasets.py`
is still an inference — a careful one, guarded by `minimum_rows` and by
`epoch_ms()` accepting whatever a timestamp column turns out to be, but an
inference. Running the probe is the cheapest outstanding item in this repo.

## Schemas

| Schema | Tables | What it looks like |
|---|---|---|
| `nyctaxi` | `trips` | **In use today.** The one the current loaders read |
| `bakehouse` | `sales_transactions`, `sales_customers`, `sales_franchises`, `sales_suppliers`, `media_customer_reviews`, `media_gold_reviews_chunked` | Retail transactions across franchises |
| `accuweather` | `historical_hourly_{metric,imperial}`, `forecast_hourly_*`, `historical_daily_calendar_*`, `forecast_daily_calendar_*`, `historical_daynight_*`, `forecast_daynight_*` | Hourly and daily weather, historical **and** forecast |
| `wanderbricks` | `bookings`, `booking_updates`, `clickstream`, `page_views`, `payments`, `properties`, `property_amenities`, `property_images`, `amenities`, `reviews`, `users`, `hosts`, `employees`, `destinations`, `countries`, `customer_support_logs` | A travel-booking business, end to end |
| `tpcds_sf1` / `tpcds_sf1000` | 24 tables each — `store_sales`, `catalog_sales`, `web_sales`, `date_dim`, `item`, `customer`, … | TPC-DS at two scale factors |
| `tpch` | `customer`, `lineitem`, `nation`, `orders`, `part`, `partsupp`, `region`, `supplier` | TPC-H |
| `healthverity` | `claims_sample_synthetic` | Synthetic health claims |
| `information_schema` | 30 views | **The useful one:** `columns` gives every column of every table in one query |

## Where the models point today, and where they arguably should

All nine models read `samples.nyctaxi.trips` — through one of the two loaders
in `models/_data/datasets.py`, `nyc_taxi_hourly()` (hourly volume and average
fare) or `nyc_taxi_trips()` (individual trips: distance, fare, duration).
That was chosen when it was the only table known to exist. Now that the
catalog is visible, some of those choices look weaker than the alternatives:

| Model | Today | Worth considering | Why |
|---|---|---|---|
| `gurobi_scheduling` | taxi hourly volume | `bakehouse.sales_transactions` + `sales_franchises` | Staffing shifts at franchises against transaction volume is what this MILP is *for*. Taxi trips are a demand curve borrowed from a business with no shifts in it |
| `gurobi_routing` | taxi trips, turned into stops | anything with real coordinates | Stops are *derived* from `trip_distance` and `duration_min` (`stops_derived_from: trip_distance_and_duration` in the results metadata), not read as locations — the sample has no coordinate columns. A table that does would make the geometry real instead of plausible. `wanderbricks.destinations` and the bakehouse franchise tables are the candidates; neither has been checked for lat/long, and this is exactly the kind of guess `scripts/probe_sample_data.py` exists to settle |
| `forecasting` | taxi hourly volume | `accuweather.historical_hourly_metric`, or `tpcds_sf1.store_sales` + `date_dim` | The taxi sample is small; a long hourly weather series or a seasonal retail series gives a forecast horizon worth having. Weather also has a *forecast* table to score against |
| `streaming_results` | taxi hourly volume | same as forecasting | A rolling-origin backtest wants length above all |
| `mcmc` | fare ~ distance | fine as-is, or `bakehouse` basket value | A real regression with a known-plausible slope; the taxi version works |
| `scenario` | taxi baseline | `wanderbricks.bookings` | Booking demand has natural scenario levers (price, capacity, season) |
| `bayesian_ab` | taxi hourly (weekend vs weekday fare) and taxi trips (long-trip speed) | `bakehouse.media_customer_reviews`, `wanderbricks.reviews` | The two comparisons are honest but contrived from a table with no A/B in it. Reviews or bookings carry a real two-arm split |
| `neural_net` | taxi trips: pace class from distance alone | fine as-is | The three usable columns are near-deterministic in each other (a taxi meter is a formula), so the model deliberately withholds the leaky ones — see `EXCLUDED_COLUMNS` in `models/neural_net/model.py`. A wider table would give it more to learn from, but the current shape is a considered choice, not a default |
| `annealing` | taxi trips as knapsack items | fine as-is | Fare as value, duration as weight, capacity as a shift length. Arbitrary but legitimate, and the point of this model is the empty dependency extra, not the data |

**None of this is worth changing on inference.** Each would need the actual
columns first, which is one probe run away. The current loaders work and are
honest about provenance; this is an improvement, not a repair.

## The one number that matters for the current loaders

`samples.nyctaxi.trips` is a *sample*, not the full NYC dataset. Aggregated to
hourly it appears to yield only a few hundred rows, not the 1,440 a
`days=60` request implies. Every loader handles a short return correctly (the
`minimum_rows` guard falls back rather than running a model on scraps), but a
run that quietly used 700 real rows where 1,440 were asked for is worth knowing
about. `scripts/probe_sample_data.py` reports the real figure.
