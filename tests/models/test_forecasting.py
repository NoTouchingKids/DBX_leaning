"""Training-loop telemetry — epochs and losses, not solver gaps.

If a generic progress view cannot render this model without special-casing,
the envelope itself needs revisiting. That is what this model is here to test.

The series is hourly NYC taxi volume from the `samples` catalog, which is not
there when these tests run — so every test below is also a test that the
fallback path works, and that a run says which of the two it was.
"""

from __future__ import annotations

import math

import pytest

pytest.importorskip("sklearn", reason="needs the [forecasting] extra")

from models._data import Dataset, nyc_taxi_hourly  # noqa: E402
from models._data.datasets import TAXI_TRIPS_TABLE  # noqa: E402
from models.forecasting import build_model  # noqa: E402
from models.forecasting import model as forecasting_model  # noqa: E402

PROVENANCE_FIELDS = {
    "data_source",
    "data_synthetic",
    "data_rows",
    "data_fallback_reason",
}


def fake_loader(rows, *, source=TAXI_TRIPS_TABLE, synthetic=False, reason=None):
    """A stand-in for the shared loader, so a test can pretend it is on a
    workspace without one."""

    def _load(**_kwargs):
        return Dataset(rows=list(rows), source=source, synthetic=synthetic, reason=reason)

    return _load


@pytest.fixture
def taxi_rows():
    """Rows shaped exactly like the real table — that is what the fallback is."""
    return nyc_taxi_hourly(days=20).rows


def test_runs_standalone_and_forecasts_the_right_shape(recorder):
    r = recorder()
    model = r.attach(build_model({"epochs": 15, "horizon": 24}))
    model.build()
    model.run()
    rows = model.results()

    assert len(rows) == 24
    assert [row["step"] for row in rows] == list(range(24))
    assert all(math.isfinite(row["forecast"]) for row in rows), "NaNs in the forecast"
    r.validate_all()


def test_it_forecasts_hourly_taxi_volume_not_a_sine_wave(recorder):
    r = recorder()
    model = r.attach(build_model({"epochs": 5, "days": 30}))
    model.build()

    assert model.column == "trips"
    assert model.data is not None, "the model should have gone to the shared loader"
    assert len(model.series) == 30 * 24
    assert model.data.rows[0].keys() >= {"hour_ts", "trips"}
    assert all(v > 0 for v in model.series), "trip counts are positive"


def test_it_runs_offline_on_the_fallback(recorder):
    """No workspace is the normal state for a contributor and for CI."""
    r = recorder()
    model = r.attach(build_model({"epochs": 10, "horizon": 6, "days": 20}))
    model.build()
    model.run()
    rows = model.results()

    assert model.data.synthetic is True
    assert "no Spark session" in (model.data.reason or "")
    assert len(rows) == 6
    assert all(math.isfinite(row["forecast"]) for row in rows)
    r.validate_all()


def test_the_provenance_is_logged_at_the_input_phase(recorder):
    r = recorder()
    model = r.attach(build_model({"epochs": 1, "days": 20}))
    model.build()

    input_logs = [line["message"] for line in r.of("log") if line["phase"] == "input"]
    assert any(model.data.provenance in message for message in input_logs), input_logs
    assert any("synthetic" in message for message in input_logs)


def test_every_result_row_carries_the_provenance(recorder):
    """Nobody should have to guess later which data a run was looking at."""
    r = recorder()
    model = r.attach(build_model({"epochs": 5, "horizon": 4, "days": 20}))
    model.build()
    model.run()
    rows = model.results()

    for row in rows:
        assert PROVENANCE_FIELDS <= set(row)
        assert row["data_synthetic"] is True
        assert row["data_source"] == model.data.source
        assert row["data_rows"] == len(model.data)
        assert "no Spark session" in row["data_fallback_reason"]
    # One shape regardless of how the run went — the results table has fixed
    # columns, so an absent fallback reason must be a NULL, not a missing key.
    assert len({tuple(sorted(row)) for row in rows}) == 1


def test_a_real_run_and_a_fallback_run_are_distinguishable(recorder, taxi_rows, monkeypatch):
    """The whole point of carrying provenance: these two must not look alike."""
    monkeypatch.setattr(forecasting_model, "nyc_taxi_hourly", fake_loader(taxi_rows))
    r = recorder()
    model = r.attach(build_model({"epochs": 5, "horizon": 4}))
    model.build()
    model.run()
    row = model.results()[0]

    assert row["data_synthetic"] is False
    assert row["data_source"] == TAXI_TRIPS_TABLE
    assert row["data_fallback_reason"] is None
    assert PROVENANCE_FIELDS <= set(row)

    messages = [line["message"] for line in r.of("log") if line["phase"] == "input"]
    assert any(TAXI_TRIPS_TABLE in message for message in messages), messages
    assert not any("synthetic" in message for message in messages)


def test_rows_the_real_table_left_null_are_dropped_loudly(recorder, taxi_rows, monkeypatch):
    """A real table has NULLs in it; a lag window built on one is worthless."""
    holed = [dict(row) for row in taxi_rows]
    holed[5]["trips"] = None
    monkeypatch.setattr(forecasting_model, "nyc_taxi_hourly", fake_loader(holed))

    r = recorder()
    model = r.attach(build_model({"epochs": 2}))
    model.build()

    assert len(model.series) == len(holed) - 1
    assert any(
        line["level"] == "WARNING" and "dropped 1 rows" in line["message"]
        for line in r.of("log")
    )


def test_a_caller_can_still_bring_its_own_series(recorder):
    series = [50.0 + 10.0 * math.sin(i / 3.0) for i in range(300)]
    r = recorder()
    model = r.attach(build_model({"series": series, "epochs": 5, "horizon": 3}))
    model.build()
    model.run()
    rows = model.results()

    assert model.series == series
    assert model.data is None, "a supplied series must not trigger a data load"
    assert rows[0]["data_source"] == "config:series"
    assert rows[0]["ts"] is None, "no timestamps were supplied, so none are invented"
    assert len(rows) == 3


def test_the_forecast_is_better_than_predicting_the_mean(recorder):
    r = recorder()
    model = r.attach(build_model({"epochs": 60}))
    model.build()
    model.run()

    series = model.series
    baseline = sum(abs(v - sum(series) / len(series)) for v in series) / len(series)
    assert model.results()[0]["val_mae"] < baseline


def test_it_beats_the_mean_on_the_held_out_window_specifically(recorder):
    """The honest version: train-mean predictions over the *validation* window,
    on a series with real daily and weekly seasonality."""
    r = recorder()
    model = r.attach(build_model({"epochs": 60}))
    model.build()
    model.run()

    train = model.series[: int(len(model.series) * 0.8)]
    held_out = model.series[int(len(model.series) * 0.8) :]
    train_mean = sum(train) / len(train)
    baseline = sum(abs(v - train_mean) for v in held_out) / len(held_out)
    assert model.results()[0]["val_mae"] < baseline


def test_progress_is_renderable_by_a_view_that_knows_nothing_about_forecasting(recorder):
    r = recorder()
    model = r.attach(build_model({"epochs": 10}))
    model.build()
    model.run()

    progress = r.of("progress")
    assert len(progress) == 10  # at most once per epoch
    for p in progress:
        assert 0 < p["percent_complete"] <= 100
        assert p["primary_metric"] is not None
        assert p["primary_metric_label"] == "val_loss"
    assert progress[-1]["percent_complete"] == 100.0


def test_the_richer_view_gets_its_extras_in_payload(recorder):
    r = recorder()
    model = r.attach(build_model({"epochs": 5}))
    model.build()
    model.run()

    payload = r.of("progress")[-1]["payload"]
    assert {"epoch", "train_loss", "best_val_loss", "learning_rate"} <= set(payload)
    # So a live view can badge a fallback run before any results exist.
    assert payload["data_synthetic"] is True


def test_validation_loss_actually_improves(recorder):
    r = recorder()
    model = r.attach(build_model({"epochs": 40}))
    model.build()
    model.run()

    losses = [h["val_loss"] for h in model.history]
    assert losses[-1] < losses[0], "training did nothing"


def test_cancelling_mid_training_still_produces_a_forecast(recorder):
    r = recorder(cancel_after=6)
    model = r.attach(build_model({"epochs": 100, "horizon": 12}))
    model.build()
    model.run()

    assert len(model.history) < 100, "cancellation was ignored"
    rows = model.results()
    assert len(rows) == 12, "a cancelled run must still forecast from its best checkpoint"
    assert all(math.isfinite(row["forecast"]) for row in rows)
    assert PROVENANCE_FIELDS <= set(rows[0]), "a cancelled run still says where its data came from"


def test_the_best_checkpoint_is_kept_not_the_last_epoch(recorder):
    r = recorder()
    model = r.attach(build_model({"epochs": 30}))
    model.build()
    model.run()

    assert model.best_val == pytest.approx(min(h["val_loss"] for h in model.history))


def test_the_forecast_continues_the_hourly_grid(recorder):
    r = recorder()
    model = r.attach(build_model({"epochs": 5, "horizon": 5, "days": 20}))
    model.build()
    model.run()
    rows = model.results()

    hour_ms = 3_600_000
    assert rows[0]["ts"] == model.timestamps[-1] + hour_ms
    assert [row["ts"] for row in rows] == [
        model.timestamps[-1] + (i + 1) * hour_ms for i in range(5)
    ]


def test_the_data_and_the_forecast_are_deterministic(recorder):
    """Same seed, same rows, same forecast — the fallback must not drift."""
    forecasts = []
    for _ in range(2):
        r = recorder()
        model = r.attach(build_model({"epochs": 8, "horizon": 4, "days": 20}))
        model.build()
        model.run()
        forecasts.append([row["forecast"] for row in model.results()])
    assert forecasts[0] == forecasts[1]

    assert (
        nyc_taxi_hourly(days=2, seed=1).rows != nyc_taxi_hourly(days=2, seed=2).rows
    ), "a different seed should give different data"


def test_the_harness_sees_a_build_step_and_a_results_accessor():
    from job.loader import describe_object

    handle = describe_object(build_model(), "models.forecasting")
    assert handle.build is not None and handle.run is not None
    assert handle.results is not None and handle.results_table == "results_forecasting"
