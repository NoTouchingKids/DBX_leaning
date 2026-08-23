#!/usr/bin/env python3
"""Report which `samples` tables this workspace actually has, and their shape.

Run this **on a Databricks workspace** (a notebook or a job) before trusting
any model's data assumptions. The loaders in `models/_data` are written
against Databricks' documented sample data and fall back cleanly when it is
absent — but "falls back cleanly" and "is actually reading real data" are
different states, and only this can tell you which one you are in.

    databricks jobs submit ... entrypoints/... , or paste into a notebook cell
"""

from __future__ import annotations

import sys

TABLES = [
    "samples.nyctaxi.trips",
    "samples.tpch.orders",
    "samples.tpch.lineitem",
    "samples.tpch.customer",
]


def main() -> int:
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
    from models._data import samples_available, spark_session
    from models._data.datasets import nyc_taxi_hourly, nyc_taxi_trips

    spark = spark_session()
    if spark is None:
        print("No Spark session — run this on a Databricks cluster, not locally.")
        return 2

    print(f"samples catalog available: {samples_available(spark)}\n")

    for table in TABLES:
        try:
            count = spark.sql(f"SELECT COUNT(*) AS n FROM {table}").collect()[0]["n"]
            columns = [f.name for f in spark.table(table).schema.fields]
            print(f"{table}\n  rows: {count:,}\n  columns: {', '.join(columns)}\n")
        except Exception as exc:  # noqa: BLE001
            print(f"{table}\n  UNAVAILABLE: {type(exc).__name__}: {exc}\n")

    print("--- what the model loaders actually get ---")
    for name, loader in (("nyc_taxi_hourly", nyc_taxi_hourly), ("nyc_taxi_trips", nyc_taxi_trips)):
        data = loader()
        print(f"{name}: {data.provenance}")
        if data.rows:
            print(f"  first row: {data.rows[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
