"""Training-loop telemetry — epochs and losses, not solver gaps.

If a generic progress view cannot render this model without special-casing,
the envelope itself needs revisiting. That is what this model is here to test.
"""

from __future__ import annotations

import math

import pytest

pytest.importorskip("sklearn", reason="needs the [forecasting] extra")

from models.forecasting import build_model, synthetic_series  # noqa: E402


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


def test_the_forecast_is_better_than_predicting_the_mean(recorder):
    r = recorder()
    model = r.attach(build_model({"epochs": 60}))
    model.build()
    model.run()

    series = model.series
    baseline = sum(abs(v - sum(series) / len(series)) for v in series) / len(series)
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


def test_the_best_checkpoint_is_kept_not_the_last_epoch(recorder):
    r = recorder()
    model = r.attach(build_model({"epochs": 30}))
    model.build()
    model.run()

    assert model.best_val == pytest.approx(min(h["val_loss"] for h in model.history))


def test_the_series_is_deterministic_for_a_seed():
    assert synthetic_series(n=50, seed=1) == synthetic_series(n=50, seed=1)
    assert synthetic_series(n=50, seed=1) != synthetic_series(n=50, seed=2)


def test_the_harness_sees_a_build_step_and_a_results_accessor():
    from job.loader import describe_object

    handle = describe_object(build_model(), "models.forecasting")
    assert handle.build is not None and handle.run is not None
    assert handle.results is not None and handle.results_table == "results_forecasting"
