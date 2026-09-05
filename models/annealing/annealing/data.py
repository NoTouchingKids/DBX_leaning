"""This model's data lifecycle: read the trips, write the shift.

**A model owns its own data.** The harness moves telemetry and nothing else —
`job/delta.py` was deleted rather than reduced, so there is no writer to borrow
and no table to be told about. Everything that touches Unity Catalog for this
model is in this one file, both directions:

    nyc_taxi_trips()   the knapsack's items, read through Spark
    write_rows()       the chosen shift, appended through Spark

Both degrade off a workspace, and the two degradations are deliberately not
symmetric:

- A **read** with no Spark session falls back to a deterministic generator, so
  the model runs on a laptop. The fallback is real data of the same shape, not
  a stub — a knapsack over uniform random numbers is solved by greed and would
  prove nothing about the search.
- A **write** with no Spark session writes nothing and says so, returning
  `None` rather than a row count. Pretending is the one thing it must not do:
  `row_count` is what distinguishes "succeeded, wrote 41 rows" from
  "succeeded, wrote nothing because the write failed", and a write that
  reported success over a laptop would make that field meaningless.

**Spark, not the SQL warehouse.** A serverless job already has a session, so
reading a UC table there costs nothing extra. Going through the warehouse would
wake it for the duration of the read, and warehouse cost is driven by uptime —
the exact mistake this platform exists to avoid.

**`pyspark` is not in this model's dependency list and must not be.** The
serverless environment provides it, exactly as it provides `modelkit`; the
imports below are inside functions so that importing this package on a laptop
loads nothing but the standard library.

## What was left behind

This is a port of `job/models/_data/` (on `dev`), which served ten models and
carried what all of them needed together: an hourly demand loader, an epoch-ms
timestamp coercion, `Dataset.floats`/`.column`, a `samples_available` probe.
None of it is here, because none of it is used here — and a shared `_data`
package is precisely what "a model owns its own data" replaces. A second model
wanting the hourly loader ports it into its own package, and the two copies are
then free to disagree, which is the point rather than the cost.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

__all__ = ["Dataset", "nyc_taxi_trips", "spark_session", "write_rows", "TAXI_TRIPS_TABLE"]

#: Databricks' read-only sample data. Present on Free Edition; a workspace can
#: still have it disabled, which is why nothing here assumes it. Verified
#: present on 2026-08-23 (`docs/sample-data-inventory.md`, on `dev`).
TAXI_TRIPS_TABLE = "samples.nyctaxi.trips"


@dataclass(frozen=True)
class Dataset:
    """Rows plus where they came from.

    Provenance is part of the result rather than a log line: a run that read
    real trips and a run that fell back to the generator must not look
    identical afterwards. `describe()` is what carries that onto every result
    row.
    """

    rows: list[dict[str, Any]]
    #: The table this came from, or a `synthetic:` name.
    source: str
    synthetic: bool
    #: Why the real table was not used, when it was not. None on success.
    reason: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.rows)

    def dropna(self, *columns: str) -> Dataset:
        """Rows where every named column is present and finite.

        Returns a new Dataset so provenance travels with the filtered rows —
        including a row count that reflects what the model actually used
        rather than what the table happened to contain.

        This is the guard against the failure that only ever happens on a
        workspace: a real `fare_amount` can be NULL, `float(None)` raises a
        bare TypeError from deep inside the search, and no offline run ever
        produces one.
        """
        if not self.rows:
            return self

        wanted = columns or tuple(self.rows[0])

        def usable(row: dict[str, Any]) -> bool:
            for name in wanted:
                value = row.get(name)
                if value is None:
                    return False
                if isinstance(value, float) and not math.isfinite(value):
                    return False
            return True

        kept = [row for row in self.rows if usable(row)]
        dropped = len(self.rows) - len(kept)
        meta = dict(self.meta)
        if dropped:
            meta["rows_dropped"] = meta.get("rows_dropped", 0) + dropped
        return Dataset(
            rows=kept,
            source=self.source,
            synthetic=self.synthetic,
            reason=self.reason,
            meta=meta,
        )

    @property
    def provenance(self) -> str:
        """One line a model can put straight into a log message."""
        if self.synthetic:
            return f"synthetic data ({self.source}): {self.reason or 'no reason given'}"
        return f"{len(self.rows)} rows from {self.source}"

    def describe(self) -> dict[str, Any]:
        """Provenance as fields, for a result row or an envelope payload.

        Always the same keys, including ``data_fallback_reason: None`` on a
        successful read. An earlier version omitted the key on success, which
        gave one results table two different row schemas depending on how the
        run went.
        """
        return {
            "data_source": self.source,
            "data_synthetic": self.synthetic,
            "data_rows": len(self.rows),
            "data_fallback_reason": self.reason,
        }


def spark_session() -> Any | None:
    """The job's existing Spark session, or None when running off-platform.

    Imported inside the function rather than at module scope so that a laptop,
    a notebook with no cluster attached and the test suite can all import this
    package for free. `None` is a normal state here, not a degraded one.
    """
    try:
        from pyspark.sql import SparkSession
    except ImportError:
        return None
    try:
        return SparkSession.getActiveSession() or SparkSession.builder.getOrCreate()
    except Exception:  # noqa: BLE001 - no session is a normal local state
        return None


def query(sql: str) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Run ``sql`` through Spark. Returns ``(rows, reason_it_failed)``.

    Never raises. A model must not fail because its sample data was
    unavailable — that is what the fallback is for — and the reason travels
    with the substituted rows so the substitution stays visible afterwards.
    """
    spark = spark_session()
    if spark is None:
        return None, "no Spark session (not running on a Databricks cluster)"
    try:
        return [row.asDict(recursive=True) for row in spark.sql(sql).collect()], None
    except Exception as exc:  # noqa: BLE001 - an unreadable table is not a failed run
        reason = f"{type(exc).__name__}: {exc}"
        log.info("sample-data query failed, falling back to synthetic (%s)", reason)
        return None, reason


def load(
    sql: str,
    *,
    source: str,
    fallback: Callable[[], Sequence[dict[str, Any]]],
    fallback_name: str,
    minimum_rows: int = 1,
) -> Dataset:
    """Real rows if they are there, generated ones if not.

    ``minimum_rows`` guards the case that actually bites: a table that exists
    but comes back nearly empty, which would otherwise produce a model that
    "ran fine" on four trips.
    """
    rows, reason = query(sql)

    if rows is not None and len(rows) < minimum_rows:
        reason = f"{source} returned {len(rows)} rows, need at least {minimum_rows}"
        log.info("%s; falling back to synthetic", reason)
        rows = None

    if rows is None:
        return Dataset(rows=list(fallback()), source=fallback_name, synthetic=True, reason=reason)
    return Dataset(rows=rows, source=source, synthetic=False)


def nyc_taxi_trips(*, limit: int = 2000, seed: int = 11) -> Dataset:
    """Individual trips — distance, fare, duration.

    Filtered to plausible trips: the raw table contains zero-distance and
    negative-fare rows, and a knapsack over those measures data quality rather
    than anything about annealing.

    The filter is also what keeps the instance well-posed. Every item needs a
    positive weight and a positive value, or its value density is undefined and
    the penalty derived from the densities is meaningless.
    """
    sql = f"""
        SELECT
            trip_distance,
            fare_amount,
            (unix_timestamp(tpep_dropoff_datetime)
             - unix_timestamp(tpep_pickup_datetime)) / 60.0 AS duration_min
        FROM {TAXI_TRIPS_TABLE}
        WHERE trip_distance BETWEEN 0.1 AND 40
          AND fare_amount BETWEEN 2.5 AND 250
          AND tpep_dropoff_datetime > tpep_pickup_datetime
        LIMIT {limit}
    """
    return load(
        sql,
        source=TAXI_TRIPS_TABLE,
        fallback=lambda: _synthetic_trips(limit, seed=seed),
        fallback_name="synthetic:trips",
        # A knapsack over a handful of items is not a search. Below this the
        # generator is the better instance, even if the table technically read.
        minimum_rows=100,
    )


def _synthetic_trips(n: int, *, seed: int) -> list[dict[str, Any]]:
    """The fallback, and it is a real one.

    Same columns and the same broad statistics as the table: a lognormal
    distance, a duration that grows with it, and a fare that grows with it
    differently. **The correlation is the whole point.** Weights and values
    that move together the way real ones do is what makes this knapsack a
    search worth running — over uniform random numbers it is solved by greed,
    and the annealing would look impressive while proving nothing.

    Deterministic for a seed, and its own `random.Random` rather than the
    module-level generator, which is shared process-wide: anything else in the
    job touching `random` would otherwise change this run's instance.
    """
    import random

    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        distance = round(min(40.0, max(0.1, rng.lognormvariate(0.6, 0.7))), 4)
        duration = round(max(1.0, 3.0 + distance * 3.2 + rng.gauss(0, 2.0)), 4)
        fare = round(min(250.0, max(2.5, 3.0 + 2.6 * distance + rng.gauss(0, 1.5))), 4)
        rows.append({"trip_distance": distance, "fare_amount": fare, "duration_min": duration})
    return rows


def write_rows(
    table: str,
    schema: Sequence[tuple[str, str]],
    rows: Sequence[dict[str, Any]],
) -> int | None:
    """Append ``rows`` to a Unity Catalog table. Returns how many were written.

    ``None`` means **nothing was written because there is no Spark session** —
    a laptop, a notebook with no cluster, the test suite. That is distinct from
    `0`, which would mean the model had nothing to say, and distinct again from
    an exception, which means the write was attempted and failed. The caller
    has to tell all three apart to report `row_count` honestly, so they are
    three outcomes rather than one falsy number.

    Anything else raises. A failed write is not a degradation to be logged and
    swallowed: results are not best-effort, and a run must not report SUCCEEDED
    over a lost one.

    ``schema`` is `(column, SQL type)` pairs and earns its place three times
    over: it fixes the column ORDER, it types the frame explicitly so a column
    that happens to be all-NULL is not inferred as something the table will
    reject, and it is the thing to diff by hand against
    `uc_ddl/002_model_results.sql`. A key missing from a row raises `KeyError`
    here rather than arriving as a silently NULL column, which is the direction
    that costs a result.

    On a workspace where the DDL has never been applied, `saveAsTable` in
    append mode CREATES the table from this schema. That is a convenience and
    not the intent: the DDL carries the comments and the NOT NULL constraints,
    and it stays the authority.
    """
    spark = spark_session()
    if spark is None:
        return None

    ddl = ", ".join(f"{name} {sql_type}" for name, sql_type in schema)
    ordered = [tuple(row[name] for name, _ in schema) for row in rows]

    spark.createDataFrame(ordered, schema=ddl).write.mode("append").saveAsTable(table)
    return len(ordered)
