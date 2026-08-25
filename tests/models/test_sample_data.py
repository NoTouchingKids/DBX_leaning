"""The sample-data loader: real rows when a workspace is there, deterministic
ones when it is not, and never a model that cannot tell the difference.
"""

from __future__ import annotations

from datetime import UTC

import pytest

from job.models._data import Dataset, nyc_taxi_hourly, nyc_taxi_trips
from job.models._data import sample_data as sd


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
    assert set(nyc_taxi_hourly(days=1).rows[0]) == {"hour_ts", "trips", "avg_fare", "avg_distance"}
    assert set(nyc_taxi_trips(limit=10).rows[0]) == {"trip_distance", "fare_amount", "duration_min"}


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
        # Present and null, not absent — see test_describe_always_has_the_same_keys.
        "data_fallback_reason": None,
    }
    assert real.provenance == "1 rows from samples.nyctaxi.trips"


def test_a_query_failure_falls_back_rather_than_raising(monkeypatch):
    monkeypatch.setattr(sd, "query", lambda sql, limit=None: (None, "TABLE_OR_VIEW_NOT_FOUND"))
    data = sd.load("SELECT 1", source="samples.nope", fallback=lambda: [{"x": 1}], minimum_rows=1)
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
    data = sd.load("SELECT 1", source="samples.t", fallback=lambda: [], minimum_rows=48)
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


# --- regressions found by the model tracks --------------------------------


def test_a_null_aggregate_gives_a_clear_error_not_a_bare_typeerror():
    """AVG() over an hour whose values are all NULL returns NULL, and
    float(None) used to raise from deep inside a model. That failure only ever
    happens on a workspace, never offline — the worst shape a bug can have."""
    data = Dataset(rows=[{"v": 1.0}, {"v": None}], source="samples.x", synthetic=False)
    with pytest.raises(ValueError, match=r"row 1: column 'v' is None"):
        data.floats("v")


def test_a_default_can_be_substituted_for_a_null():
    data = Dataset(rows=[{"v": 1.0}, {"v": None}], source="samples.x", synthetic=False)
    assert data.floats("v", default=0.0) == [1.0, 0.0]


def test_non_finite_values_are_treated_as_missing_too():
    data = Dataset(rows=[{"v": float("nan")}], source="samples.x", synthetic=False)
    with pytest.raises(ValueError):
        data.floats("v")
    assert data.dropna("v").rows == []


def test_dropna_keeps_every_column_aligned():
    """Filtering per-column in a model desynchronises parallel series; this
    drops whole rows so a timestamp never outlives its value."""
    data = Dataset(
        rows=[{"t": 1, "v": 10.0}, {"t": 2, "v": None}, {"t": 3, "v": 30.0}],
        source="samples.x",
        synthetic=False,
    )
    clean = data.dropna("v")
    assert clean.column("t") == [1, 3]
    assert clean.floats("v") == [10.0, 30.0]


def test_dropna_carries_provenance_and_reports_what_it_dropped():
    data = Dataset(
        rows=[{"v": 1.0}, {"v": None}],
        source="samples.nyctaxi.trips",
        synthetic=False,
    )
    clean = data.dropna("v")
    assert clean.source == "samples.nyctaxi.trips" and clean.synthetic is False
    assert clean.meta["rows_dropped"] == 1
    # The row count reflects what a model actually used, not what the table held.
    assert clean.describe()["data_rows"] == 1


def test_dropna_with_no_columns_named_checks_them_all():
    data = Dataset(rows=[{"a": 1, "b": None}, {"a": 2, "b": 3}], source="s", synthetic=False)
    assert data.dropna().rows == [{"a": 2, "b": 3}]


def test_describe_always_has_the_same_keys():
    """One results table must not have two row schemas depending on whether
    the run happened to fall back."""
    real = Dataset(rows=[{"a": 1}], source="samples.x", synthetic=False)
    fell_back = Dataset(rows=[{"a": 1}], source="synthetic", synthetic=True, reason="no Spark")

    assert set(real.describe()) == set(fell_back.describe())
    assert real.describe()["data_fallback_reason"] is None
    assert fell_back.describe()["data_fallback_reason"] == "no Spark"


def test_hour_ts_is_epoch_ms_whatever_the_source_returned():
    """Spark returns a datetime for a TimestampType while the fallback returns
    an int. A model doing int(row["hour_ts"]) would work offline and raise on
    a workspace — the contract has to be true on the path that matters."""
    from datetime import date, datetime

    from job.models._data.datasets import epoch_ms

    expected = 1_600_000_000_000
    assert epoch_ms(expected) == expected
    assert epoch_ms(datetime(2020, 9, 13, 12, 26, 40, tzinfo=UTC)) == expected
    assert epoch_ms("2020-09-13T12:26:40+00:00") == expected
    # A naive datetime is read as UTC rather than as local time, so the same
    # table read from two machines does not produce two different series.
    assert epoch_ms(datetime(2020, 9, 13, 12, 26, 40)) == expected
    assert epoch_ms(date(2020, 9, 13)) == 1_599_955_200_000


def test_a_boolean_is_not_quietly_accepted_as_a_timestamp():
    from job.models._data.datasets import epoch_ms

    with pytest.raises(TypeError, match="boolean"):
        epoch_ms(True)


def test_an_unreadable_timestamp_says_what_it_got():
    from job.models._data.datasets import epoch_ms

    with pytest.raises(TypeError, match="cannot read"):
        epoch_ms(object())


def test_a_workspace_returning_datetimes_still_honours_the_contract(monkeypatch):
    """Belt and braces over the SQL cast: normalise whatever comes back."""
    from datetime import datetime

    from job.models._data import datasets
    from job.models._data import sample_data as sd

    rows = [
        {
            "hour_ts": datetime(2020, 9, 13, h, tzinfo=UTC),
            "trips": 100 + h,
            "avg_fare": 12.0,
            "avg_distance": 2.5,
        }
        for h in range(24)
    ] * 3
    monkeypatch.setattr(sd, "query", lambda sql, limit=None: (rows, None))

    data = datasets.nyc_taxi_hourly(days=3)
    assert not data.synthetic
    assert all(isinstance(row["hour_ts"], int) for row in data.rows)
    # And the thing a model actually does with it now works.
    assert [int(row["hour_ts"]) for row in data.rows][:1] == [1_599_955_200_000]
