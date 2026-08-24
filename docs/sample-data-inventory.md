# What's actually in the `samples` catalog

Verified against a real Free Edition workspace on **2026-08-23** by listing
`samples.information_schema.tables`. This file exists because guessing at this
already cost us a defect: `hour_ts` was documented as epoch milliseconds on the
strength of inference, while Spark actually returns a `datetime` — invisible in
every test, and a hard failure on the first real run.

**Table names are confirmed. Columns are confirmed for seven tables and
still unverified for the other 116** — see "Confirmed columns" below, and
read the warning that follows it before trusting any absence.

Transport is not in question either: WebSocket job→app and SSE app→browser
were both exercised by hand against a real workspace before this repo existed
in its current form, which is what `docs/spike-results.md` records. What
remains unmeasured is *timings*, not whether they work.

`scripts/probe_sample_data.py` has still never been run end to end on a
workspace. Two of the three things it would answer are now settled by the
listings pasted below; the third — what the loaders actually receive, with
real values and real NULLs — is not, and is the one that has bitten this
project before. Every defect this repo has hit in this area was a *value*
problem rather than a *name* problem: `hour_ts` documented as epoch ms while
Spark returned a `datetime`, and `float(None)` on an `AVG()` over an empty
hour. A column list cannot catch either.

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

## Confirmed columns

From `samples.information_schema.columns`, listed on **2026-08-24**. The raw
listing is committed as `docs/samples-columns-2026-08-24.csv` — the tables
below are a readable extract of it, and the CSV is what to re-derive from
rather than re-typing anything by hand.

### The warning, first

`information_schema.tables` and `information_schema.columns` **do not cover
the same tables in this catalog.** `tables` lists 123 tables across nine
schemas. `columns` returned rows for seven of them: all six of `bakehouse`,
one of `accuweather`'s twelve, and nothing whatsoever for `nyctaxi`, `tpch`,
`tpcds_sf1`, `tpcds_sf1000`, `wanderbricks` or `healthverity`.

The pattern is not alphabetical, not a row cap, and not a permissions
boundary visible from here. It does not matter *why*: what matters is that
**absence from `columns` is not evidence of absence.** `samples.nyctaxi.trips`
— the table every model in this repo reads today — returns no rows from
`columns` and is listed in `tables`.

`scripts/probe_sample_data.py` originally treated the two as interchangeable
and would have reported `nyctaxi.trips` as NOT PRESENT. It now takes existence
from `tables` and falls back to `DESCRIBE TABLE` for anything `columns`
skips, because `DESCRIBE` works regardless of what the metadata views choose
to expose.

### `accuweather.historical_hourly_imperial`

| # | column | type | null |
|---|---|---|---|
| 0 | `city_name` | STRING | yes |
| 1 | `country_code` | STRING | yes |
| 2 | `latitude` | DOUBLE | yes |
| 3 | `longitude` | DOUBLE | yes |
| 4 | `date` | TIMESTAMP | yes |
| 5 | `cloud_base_height` | INT | yes |
| 6 | `cloud_cover_high` | STRING | yes |
| 7 | `cloud_cover_low` | STRING | yes |
| 8 | `cloud_cover_medium` | STRING | yes |
| 9 | `cloud_cover_total` | DOUBLE | yes |
| 10 | `humidity_relative` | DOUBLE | yes |
| 11 | `index_uv` | DOUBLE | yes |
| 12 | `has_ice` | STRING | yes |
| 13 | `ice_lwe` | STRING | yes |
| 14 | `ice_lwe_rate` | STRING | yes |
| 15 | `minutes_of_ice` | STRING | yes |
| 16 | `minutes_of_precipitation` | INT | yes |
| 17 | `minutes_of_sun` | INT | yes |
| 18 | `minutes_of_rain` | STRING | yes |
| 19 | `minutes_of_snow` | INT | yes |
| 20 | `has_precipitation` | BOOLEAN | yes |
| 21 | `precipitation_lwe` | DOUBLE | yes |
| 22 | `precipitation_lwe_rate` | DOUBLE | yes |
| 23 | `precipitation_type` | INT | yes |
| 24 | `precipitation_type_desc` | STRING | yes |
| 25 | `pressure` | DOUBLE | yes |
| 26 | `pressure_msl` | DOUBLE | yes |
| 27 | `has_rain` | STRING | yes |
| 28 | `rain_lwe` | STRING | yes |
| 29 | `rain_lwe_rate` | STRING | yes |
| 30 | `snow_cover` | STRING | yes |
| 31 | `snow_depth` | STRING | yes |
| 32 | `has_snow` | BOOLEAN | yes |
| 33 | `snow` | STRING | yes |
| 34 | `snow_lwe` | DOUBLE | yes |
| 35 | `snow_lwe_rate` | DOUBLE | yes |
| 36 | `solar_irradiance` | DOUBLE | yes |
| 37 | `solar_radiation_net` | DOUBLE | yes |
| 38 | `temperature` | DOUBLE | yes |
| 39 | `temperature_dew_point` | DOUBLE | yes |
| 40 | `temperature_heat_index` | DOUBLE | yes |
| 41 | `temperature_realfeel` | DOUBLE | yes |
| 42 | `temperature_realfeel_shade` | DOUBLE | yes |
| 43 | `temperature_wind_chill` | DOUBLE | yes |
| 44 | `visibility` | DOUBLE | yes |
| 45 | `wind_direction` | DOUBLE | yes |
| 46 | `wind_gust` | DOUBLE | yes |
| 47 | `wind_gust_instantaneous` | STRING | yes |
| 48 | `wind_speed` | DOUBLE | yes |

### `bakehouse.media_customer_reviews`

| # | column | type | null |
|---|---|---|---|
| 0 | `review` | STRING | yes |
| 1 | `franchiseID` | LONG | yes |
| 2 | `review_date` | TIMESTAMP | yes |
| 3 | `new_id` | INT | yes |

### `bakehouse.media_gold_reviews_chunked`

| # | column | type | null |
|---|---|---|---|
| 0 | `franchiseID` | INT | yes |
| 1 | `review_date` | TIMESTAMP | yes |
| 2 | `chunked_text` | STRING | yes |
| 3 | `chunk_id` | STRING | yes |
| 4 | `review_uri` | STRING | yes |

### `bakehouse.sales_customers`

| # | column | type | null |
|---|---|---|---|
| 0 | `customerID` | LONG | yes |
| 1 | `first_name` | STRING | yes |
| 2 | `last_name` | STRING | yes |
| 3 | `email_address` | STRING | yes |
| 4 | `phone_number` | STRING | yes |
| 5 | `address` | STRING | yes |
| 6 | `city` | STRING | yes |
| 7 | `state` | STRING | yes |
| 8 | `country` | STRING | yes |
| 9 | `continent` | STRING | yes |
| 10 | `postal_zip_code` | LONG | yes |
| 11 | `gender` | STRING | yes |

### `bakehouse.sales_franchises`

| # | column | type | null |
|---|---|---|---|
| 0 | `franchiseID` | LONG | yes |
| 1 | `name` | STRING | yes |
| 2 | `city` | STRING | yes |
| 3 | `district` | STRING | yes |
| 4 | `zipcode` | STRING | yes |
| 5 | `country` | STRING | yes |
| 6 | `size` | STRING | yes |
| 7 | `longitude` | DOUBLE | yes |
| 8 | `latitude` | DOUBLE | yes |
| 9 | `supplierID` | LONG | yes |

### `bakehouse.sales_suppliers`

| # | column | type | null |
|---|---|---|---|
| 0 | `supplierID` | LONG | yes |
| 1 | `name` | STRING | yes |
| 2 | `ingredient` | STRING | yes |
| 3 | `continent` | STRING | yes |
| 4 | `city` | STRING | yes |
| 5 | `district` | STRING | yes |
| 6 | `size` | STRING | yes |
| 7 | `longitude` | DOUBLE | yes |
| 8 | `latitude` | DOUBLE | yes |
| 9 | `approved` | STRING | yes |

### `bakehouse.sales_transactions`

| # | column | type | null |
|---|---|---|---|
| 0 | `transactionID` | LONG | yes |
| 1 | `customerID` | LONG | yes |
| 2 | `franchiseID` | LONG | yes |
| 3 | `dateTime` | TIMESTAMP | yes |
| 4 | `product` | STRING | yes |
| 5 | `quantity` | LONG | yes |
| 6 | `unitPrice` | LONG | yes |
| 7 | `totalPrice` | LONG | yes |
| 8 | `paymentMethod` | STRING | yes |
| 9 | `cardNumber` | LONG | yes |

### What this settles

**`gurobi_routing` can have real coordinates.** The open question in the table
below was whether anything in `samples` carries lat/long. `bakehouse.sales_franchises`
and `bakehouse.sales_suppliers` both do, as `DOUBLE` — so a routing instance
over franchise or supplier locations would be real geometry rather than
positions derived from `trip_distance`. `accuweather.historical_hourly_imperial`
carries them too, per city.

**`bakehouse.sales_transactions` supports the scheduling rework.** It has
`dateTime TIMESTAMP`, `franchiseID`, `quantity` and `totalPrice`, which is
what a demand curve per franchise per hour needs — and `sales_franchises`
joins to it on `franchiseID` with a `size` column that plausibly bounds staff.

One trap in it: `unitPrice`, `totalPrice` and `quantity` are all `LONG`, not
decimal. Money as an integer is fine to sum but is almost certainly minor
units or a rounded value; check the magnitudes before dividing by anything.

## `samples` is no longer the only option

Lifted 2026-08-24: a model may read external data where that genuinely suits
it, not only this catalog. What that does and does not change:

- **Does not change the code.** `models/_data.load()` takes arbitrary SQL and
  a `source` label; it was never samples-specific. A new loader in
  `datasets.py` is the entire job.
- **Does not change the egress rule.** Nothing may be fetched over the
  internet at run time. External data has to reach Unity Catalog first — a
  volume, a Marketplace product, Delta Sharing. `docs/free-edition-constraints.md`
  has the routes.
- **Does not change the fallback rule.** A synthetic fallback stays mandatory
  whatever the source, because it is what keeps a model runnable in tests and
  on a laptop.

So the table below is now a list of *candidates within `samples`*, not a list
of the only choices. Several of its "worth considering" entries were written
under the old restriction and may have better answers outside the catalog.

## Where the models point today, and where they arguably should

All nine models read `samples.nyctaxi.trips` — through one of the two loaders
in `models/_data/datasets.py`, `nyc_taxi_hourly()` (hourly volume and average
fare) or `nyc_taxi_trips()` (individual trips: distance, fare, duration).
That was chosen when it was the only table known to exist. Now that the
catalog is visible, some of those choices look weaker than the alternatives:

| Model | Today | Worth considering | Why |
|---|---|---|---|
| `gurobi_scheduling` | taxi hourly volume | `bakehouse.sales_transactions` + `sales_franchises` | Staffing shifts at franchises against transaction volume is what this MILP is *for*. Taxi trips are a demand curve borrowed from a business with no shifts in it |
| `gurobi_routing` | taxi trips, turned into stops | `bakehouse.sales_franchises` or `sales_suppliers` — **settled, both carry lat/long** | Stops are *derived* from `trip_distance` and `duration_min` (`stops_derived_from: trip_distance_and_duration` in the results metadata), not read as locations — the taxi sample has no coordinate columns. This was the open question and the 2026-08-24 column listing answers it: `sales_franchises` and `sales_suppliers` both have `longitude DOUBLE` / `latitude DOUBLE`. A routing instance over those is real geometry rather than plausible geometry. `wanderbricks.destinations` remains unchecked — `information_schema.columns` returns nothing for that schema |
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
