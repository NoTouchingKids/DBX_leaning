"""The specific `samples` tables this platform's models use.

One function per dataset rather than per model, so two models wanting NYC taxi
hourly volumes ask the same question and get the same shape.

Every one takes a ``fallback`` and returns a :class:`Dataset`, so a model
never has to branch on whether it is running on a workspace.
"""

from __future__ import annotations

import math
from typing import Any

from .sample_data import Dataset, load

__all__ = ["nyc_taxi_hourly", "nyc_taxi_trips", "TAXI_TRIPS_TABLE"]

#: Databricks' standard sample dataset. Read-only, present on Free Edition.
TAXI_TRIPS_TABLE = "samples.nyctaxi.trips"


def nyc_taxi_hourly(*, days: int = 60, seed: int = 7) -> Dataset:
    """Hourly trip counts and average fare — a real demand curve.

    Genuinely seasonal at two scales (daily and weekly), which is what makes
    it worth forecasting and worth staffing against, rather than a sine wave
    that any model fits perfectly.
    """
    sql = f"""
        SELECT
            date_trunc('HOUR', tpep_pickup_datetime) AS hour_ts,
            COUNT(*)                                 AS trips,
            AVG(fare_amount)                         AS avg_fare,
            AVG(trip_distance)                       AS avg_distance
        FROM {TAXI_TRIPS_TABLE}
        WHERE tpep_pickup_datetime IS NOT NULL
        GROUP BY 1
        HAVING COUNT(*) > 0
        ORDER BY 1
        LIMIT {days * 24}
    """
    return load(
        sql,
        source=TAXI_TRIPS_TABLE,
        fallback=lambda: _synthetic_hourly(days * 24, seed=seed),
        fallback_name="synthetic:hourly-demand",
        minimum_rows=48,
    )


def nyc_taxi_trips(*, limit: int = 2000, seed: int = 11) -> Dataset:
    """Individual trips — distance, fare, duration. For regression-shaped work.

    Filtered to plausible trips: the raw table contains zero-distance and
    negative-fare rows, and a model fitting those is measuring data quality
    rather than anything about its own method.
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
        minimum_rows=100,
    )


# --- fallbacks -------------------------------------------------------------
# Deterministic for a seed, and shaped like the real thing: same columns, same
# broad statistics. A model must behave the same way on either.


def _rng(seed: int):
    import random

    return random.Random(seed)


def _synthetic_hourly(n: int, *, seed: int) -> list[dict[str, Any]]:
    rng = _rng(seed)
    rows = []
    base_ms = 1_600_000_000_000  # a fixed epoch; no wall-clock in a fallback
    for i in range(n):
        hour_of_day = i % 24
        day_of_week = (i // 24) % 7
        daily = 1.0 + 0.6 * math.sin(2 * math.pi * (hour_of_day - 8) / 24)
        weekly = 1.0 + 0.2 * math.sin(2 * math.pi * day_of_week / 7)
        trips = max(1, int(400 * daily * weekly + rng.gauss(0, 25)))
        rows.append(
            {
                "hour_ts": base_ms + i * 3_600_000,
                "trips": trips,
                "avg_fare": round(12.0 + 3.0 * daily + rng.gauss(0, 0.8), 4),
                "avg_distance": round(2.6 + 0.8 * daily + rng.gauss(0, 0.2), 4),
            }
        )
    return rows


def _synthetic_trips(n: int, *, seed: int) -> list[dict[str, Any]]:
    rng = _rng(seed)
    rows = []
    for _ in range(n):
        distance = round(min(40.0, max(0.1, rng.lognormvariate(0.6, 0.7))), 4)
        duration = round(max(1.0, 3.0 + distance * 3.2 + rng.gauss(0, 2.0)), 4)
        fare = round(min(250.0, max(2.5, 3.0 + 2.6 * distance + rng.gauss(0, 1.5))), 4)
        rows.append(
            {"trip_distance": distance, "fare_amount": fare, "duration_min": duration}
        )
    return rows
