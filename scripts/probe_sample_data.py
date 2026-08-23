#!/usr/bin/env python3
"""Inventory the `samples` catalog: tables, columns, row counts, and what the
model loaders actually get.

Run this **on a Databricks workspace** (notebook or job). Its whole purpose is
to stop this repo guessing at schemas — a guess already cost us one defect that
was invisible in every test and would only have failed on a real run
(`hour_ts` documented as epoch ms while Spark returned a datetime).

    uv run python scripts/probe_sample_data.py            # candidates only
    uv run python scripts/probe_sample_data.py --all      # every table
    uv run python scripts/probe_sample_data.py --counts   # add row counts (slower)

Paste the output back into the repo issue/PR so the loaders can be written
against fact rather than inference.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

#: Verified present 2026-08-23 (see docs/sample-data-inventory.md). These are
#: the ones plausibly useful to this platform's models; --all covers the rest.
CANDIDATES = [
    # Confirmed in use today.
    "samples.nyctaxi.trips",
    # Retail transactions + franchises: a far more natural fit for staff
    # scheduling than taxi trips, if the columns support it.
    "samples.bakehouse.sales_transactions",
    "samples.bakehouse.sales_franchises",
    "samples.bakehouse.sales_customers",
    # Long hourly series — the obvious forecasting/backtest candidates.
    "samples.accuweather.historical_hourly_metric",
    "samples.accuweather.forecast_hourly_metric",
    # Booking/clickstream demand.
    "samples.wanderbricks.bookings",
    "samples.wanderbricks.page_views",
    # Big, seasonal, well-understood.
    "samples.tpcds_sf1.store_sales",
    "samples.tpcds_sf1.date_dim",
    "samples.tpch.orders",
    "samples.tpch.lineitem",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="every table, not just candidates")
    parser.add_argument("--counts", action="store_true", help="COUNT(*) each table (slower)")
    args = parser.parse_args()

    from models._data import samples_available, spark_session
    from models._data.datasets import nyc_taxi_hourly, nyc_taxi_trips

    spark = spark_session()
    if spark is None:
        print("No Spark session — run this on a Databricks cluster, not locally.")
        return 2

    print(f"samples catalog available: {samples_available(spark)}\n")

    # One query for every column of every table beats guessing, and beats a
    # DESCRIBE per table.
    try:
        columns = spark.sql(
            """
            SELECT table_schema, table_name, column_name, full_data_type, ordinal_position
            FROM samples.information_schema.columns
            ORDER BY table_schema, table_name, ordinal_position
            """
        ).collect()
    except Exception as exc:  # noqa: BLE001
        print(f"could not read samples.information_schema.columns: {exc}")
        return 1

    by_table: dict[str, list[tuple[str, str]]] = {}
    for row in columns:
        key = f"samples.{row['table_schema']}.{row['table_name']}"
        by_table.setdefault(key, []).append((row["column_name"], row["full_data_type"]))

    wanted = sorted(by_table) if args.all else [t for t in CANDIDATES if t in by_table]
    missing = [t for t in CANDIDATES if t not in by_table]

    print(f"{len(by_table)} tables in samples; reporting {len(wanted)}\n")
    for table in wanted:
        print(table)
        if args.counts:
            try:
                n = spark.sql(f"SELECT COUNT(*) AS n FROM {table}").collect()[0]["n"]
                print(f"  rows: {n:,}")
            except Exception as exc:  # noqa: BLE001
                print(f"  rows: UNAVAILABLE ({type(exc).__name__})")
        for name, dtype in by_table[table]:
            print(f"    {name:<32} {dtype}")
        print()

    if missing:
        print("candidates NOT present:")
        for table in missing:
            print(f"  {table}")
        print()

    print("--- what the model loaders actually get ---")
    for name, loader in (("nyc_taxi_hourly", nyc_taxi_hourly), ("nyc_taxi_trips", nyc_taxi_trips)):
        data = loader()
        print(f"{name}: {data.provenance}")
        if data.rows:
            print(f"  first row: {data.rows[0]}")
            print(f"  types: { {k: type(v).__name__ for k, v in data.rows[0].items()} }")
    return 0


if __name__ == "__main__":
    sys.exit(main())
