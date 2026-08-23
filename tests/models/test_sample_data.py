"""The sample-data loader: real rows when a workspace is there, deterministic
ones when it is not, and never a model that cannot tell the difference.
"""

from __future__ import annotations

import pytest

from models._data import Dataset, nyc_taxi_hourly, nyc_taxi_trips
from models._data import sample_data as sd


def test_no_workspace_means_synthetic_not_an_error():
    data = nyc_taxi_hourly(days=2)
    assert data.synthetic is True
    assert len(data) == 48
    assert "no Spark session" in (data.reason or "")


def test_the_fallback_is_deterministic():
    assert nyc_taxi_hourly(days=2, seed=5).rows == nyc_taxi_hourly(days=2, seed=5).rows
    assert nyc_taxi_hourly(days=2, seed=5).rows != nyc_taxi_hourly(days=2, seed=6).rows


def test_the_fallback_has_the_same_columns_as_the_real_table():
    """A model must behave identically on either, which starts with shape."""
    assert set(nyc_taxi_hourly(days=1).rows[0]) == {
        "hour_ts", "trips", "avg_fare", "avg_distance"
    }
    assert set(nyc_taxi_trips(limit=10).rows[0]) == {
        "trip_distance", "fare_amount", "duration_min"
    }


def test_provenance_is_carried_not_hidden():
    """A run on real data and a run that fell back must not look identical."""
    data = nyc_taxi_hourly(days=1)
    described = data.describe()
    assert described["data_synthetic"] is True
    assert described["data_rows"] == 24
    assert "data_fallback_reason" in described
    assert "synthetic" in data.provenance


def test_real_data_reports_itself_as_real():
    real = Dataset(rows=[{"a": 1}], source="samples.nyctaxi.trips", synthetic=False)
    assert real.describe() == {
        "data_source": "samples.nyctaxi.trips",
        "data_synthetic": False,
        "data_rows": 1,
    }
    assert real.provenance == "1 rows from samples.nyctaxi.trips"


def test_a_query_failure_falls_back_rather_than_raising(monkeypatch):
    monkeypatch.setattr(sd, "query", lambda sql, limit=None: (None, "TABLE_OR_VIEW_NOT_FOUND"))
    data = sd.load(
        "SELECT 1", source="samples.nope", fallback=lambda: [{"x": 1}], minimum_rows=1
    )
    assert data.synthetic and data.reason == "TABLE_OR_VIEW_NOT_FOUND"


def test_a_table_that_exists_but_is_nearly_empty_falls_back(monkeypatch):
    """The case that actually bites: a model that 'ran fine' on four rows."""
    monkeypatch.setattr(sd, "query", lambda sql, limit=None: ([{"x": 1}] * 4, None))
    data = sd.load(
        "SELECT 1",
        source="samples.nyctaxi.trips",
        fallback=lambda: [{"x": 0}] * 100,
        minimum_rows=48,
    )
    assert data.synthetic is True
    assert "returned 4 rows, need at least 48" in data.reason
    assert len(data) == 100


def test_rows_are_used_when_there_are_enough_of_them(monkeypatch):
    monkeypatch.setattr(sd, "query", lambda sql, limit=None: ([{"x": i} for i in range(50)], None))
    data = sd.load(
        "SELECT 1", source="samples.t", fallback=lambda: [], minimum_rows=48
    )
    assert not data.synthetic and len(data) == 50


def test_column_helpers():
    data = Dataset(rows=[{"v": "1.5"}, {"v": "2.5"}], source="t", synthetic=False)
    assert data.floats("v") == [1.5, 2.5]
    assert data.column("v") == ["1.5", "2.5"]


def test_spark_helpers_are_safe_without_pyspark():
    assert sd.spark_session() is None
    assert sd.samples_available() is False


@pytest.mark.parametrize("loader", [nyc_taxi_hourly, nyc_taxi_trips])
def test_every_loader_returns_a_usable_dataset_offline(loader):
    data = loader()
    assert len(data) > 0 and data.synthetic
