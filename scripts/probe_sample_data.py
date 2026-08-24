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

One thing learned the hard way, and encoded below: in this catalog
`information_schema.tables` and `information_schema.columns` do not cover the
same set of tables. Existence is decided by `tables`; columns fall back to
`DESCRIBE`. See the comment in `main()`.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from typing import Any

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



#: The read-only mount Databricks ships in workspaces, separate from the
#: `samples` Unity Catalog catalog and much larger — it is where the
#: third-party CSV sample datasets live.
#:
#: Whether it is reachable from **Free Edition serverless** is the open
#: question this probe exists to answer. It is a mount rather than a fetch, so
#: if it can be listed at all it costs no egress and is a legitimate source
#: under the trusted-domains restriction. But DBFS access is restricted on
#: UC-only workspaces and on serverless compute, and nothing in this repo has
#: ever touched it, so treat an empty result as "not available here" rather
#: than "does not exist".
DATABRICKS_DATASETS = "/databricks-datasets"


def probe_databricks_datasets(spark: Any, depth: int = 1) -> None:
    """List `/databricks-datasets`, by whichever access method works.

    Three methods, tried in order, because which one is available depends on
    the compute type and none of them can be assumed:

    1. ``dbutils.fs.ls`` — the documented way, and the one most likely to
       survive on serverless.
    2. the ``/dbfs`` FUSE mount — classic clusters only; absent on serverless.
    3. Spark's ``binaryFile`` reader — works wherever Spark can read the path
       at all, and is the last resort because it is the slowest.

    Prints which method worked, because that answer is worth as much as the
    listing: it tells the next person what to write against.
    """
    print(f"--- {DATABRICKS_DATASETS} ---")

    def via_dbutils() -> list[str] | None:
        try:
            dbutils = globals().get("dbutils")
            if dbutils is None:
                from pyspark.dbutils import DBUtils  # type: ignore[import-not-found]

                dbutils = DBUtils(spark)
            return [f.path for f in dbutils.fs.ls(DATABRICKS_DATASETS)]
        except Exception as exc:  # noqa: BLE001
            print(f"  dbutils.fs.ls: unavailable ({type(exc).__name__}: {str(exc)[:120]})")
            return None

    def via_fuse() -> list[str] | None:
        import os

        path = f"/dbfs{DATABRICKS_DATASETS}"
        try:
            return sorted(os.listdir(path))
        except Exception as exc:  # noqa: BLE001
            print(f"  /dbfs FUSE mount: unavailable ({type(exc).__name__}: {str(exc)[:120]})")
            return None

    def via_spark() -> list[str] | None:
        try:
            frame = (
                spark.read.format("binaryFile")
                .option("recursiveFileLookup", "false")
                .load(f"{DATABRICKS_DATASETS}/*")
            )
            return sorted({r["path"] for r in frame.select("path").limit(500).collect()})
        except Exception as exc:  # noqa: BLE001
            print(f"  spark binaryFile: unavailable ({type(exc).__name__}: {str(exc)[:120]})")
            return None

    for name, method in (
        ("dbutils.fs.ls", via_dbutils),
        ("/dbfs FUSE", via_fuse),
        ("spark binaryFile", via_spark),
    ):
        entries = method()
        if not entries:
            continue
        print(f"  READABLE via {name} — {len(entries)} entries at the top level:")
        for entry in sorted(entries)[:80]:
            print(f"    {entry}")
        if len(entries) > 80:
            print(f"    ... and {len(entries) - 80} more")
        print(
            "\n  This is a legitimate egress-free source: it is a mount, not a "
            "download.\n  Read a CSV from it with "
            'spark.read.csv(path, header=True, inferSchema=True).'
        )
        return

    print(
        "  NOT READABLE by any method. That is a real answer, not a bug:\n"
        "  DBFS access is restricted on Unity Catalog-only workspaces and on\n"
        "  serverless compute. If this is what Free Edition does, then\n"
        "  /databricks-datasets is not a source this platform can use, and\n"
        "  external data has to arrive through a UC volume instead.\n"
        "  Record the outcome either way in docs/ml-datasets.md."
    )


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

    # Existence and columns come from two different views, because in this
    # catalog they DISAGREE.
    #
    # `information_schema.tables` lists 123 tables across nine schemas.
    # `information_schema.columns` returns rows for only some of them — as
    # observed on 2026-08-24, bakehouse in full, one of accuweather's twelve
    # tables, and nothing at all for nyctaxi, tpch, tpcds_sf1, tpcds_sf1000,
    # wanderbricks or healthverity. The pattern is not alphabetical, not a row
    # cap, and not a permissions boundary anyone here can see.
    #
    # This matters more than it sounds. An earlier version of this script
    # treated absence from `columns` as absence from the catalog, and would
    # have reported `samples.nyctaxi.trips` — the table every model in this
    # repo reads today, and which `tables` lists — as NOT PRESENT. Confidently
    # wrong output is worse than no output: it sends someone rewriting nine
    # loaders against a problem that does not exist.
    try:
        tables = spark.sql(
            """
            SELECT table_schema, table_name
            FROM samples.information_schema.tables
            ORDER BY table_schema, table_name
            """
        ).collect()
    except Exception as exc:  # noqa: BLE001
        print(f"could not read samples.information_schema.tables: {exc}")
        return 1
    present = {f"samples.{r['table_schema']}.{r['table_name']}" for r in tables}

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
        columns = []

    by_table: dict[str, list[tuple[str, str]]] = {}
    for row in columns:
        key = f"samples.{row['table_schema']}.{row['table_name']}"
        by_table.setdefault(key, []).append((row["column_name"], row["full_data_type"]))

    wanted = sorted(present) if args.all else [t for t in CANDIDATES if t in present]
    missing = [t for t in CANDIDATES if t not in present]

    # DESCRIBE works regardless of what information_schema chooses to expose,
    # so it is the fallback for every table the bulk query skipped.
    described: list[str] = []
    for table in wanted:
        if table in by_table:
            continue
        try:
            rows = spark.sql(f"DESCRIBE TABLE {table}").collect()
        except Exception as exc:  # noqa: BLE001
            print(f"  ! DESCRIBE {table} failed: {type(exc).__name__}: {exc}")
            continue
        cols = [
            (r["col_name"], r["data_type"])
            for r in rows
            # DESCRIBE appends a partition-info block after a blank row.
            if r["col_name"] and not r["col_name"].startswith("#")
        ]
        if cols:
            by_table[table] = cols
            described.append(table)

    print(
        f"{len(present)} tables in samples; {len(by_table)} with columns; "
        f"reporting {len(wanted)}"
    )
    if described:
        print(
            f"{len(described)} needed DESCRIBE because information_schema.columns "
            f"returned nothing for them: {', '.join(described)}"
        )
    print()
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
        # Absent from information_schema.TABLES, which is the authoritative
        # view. Unlike absence from `columns`, this really does mean gone.
        print("candidates NOT present (absent from information_schema.tables):")
        for table in missing:
            print(f"  {table}")
        print()

    probe_databricks_datasets(spark)
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
