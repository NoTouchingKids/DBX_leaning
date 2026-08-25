"""The heavy model: a torch classifier, and the telemetry that comes with it.

Three things this suite is really testing, none of which is accuracy:

* that a two-level training loop (batch within epoch) still produces progress
  a generic view can render with no idea what a taxi is;
* that the run is reproducible — a neural net that cannot be reproduced
  cannot be debugged;
* that the target does not leak, which is the one failure mode that makes a
  classifier look excellent and mean nothing.

The `samples` catalog is not there when these tests run, so every test below
is also a test of the deterministic fallback and of a run saying which of the
two it was.
"""

from __future__ import annotations

import math
import pathlib
import re

import pytest

pytest.importorskip("torch", reason="needs the [nn] extra")

import numpy as np  # noqa: E402

from models._data import Dataset, nyc_taxi_trips  # noqa: E402
from models._data.datasets import TAXI_TRIPS_TABLE  # noqa: E402
from models.neural_net import (  # noqa: E402
    CLASS_LABELS,
    EXCLUDED_COLUMNS,
    FEATURE_NAMES,
    build_model,
)
from models.neural_net import model as neural_net_model  # noqa: E402

PROVENANCE_FIELDS = {
    "data_source",
    "data_synthetic",
    "data_rows",
    "data_fallback_reason",
}

#: Small enough that the whole file runs in a few seconds, big enough that the
#: signal is still there.
FAST = {"limit": 1200, "epochs": 4}


def train(recorder_cls, config=None, **recorder_kwargs):
    """build + run + results, with the two callables the harness would wire."""
    r = recorder_cls(**recorder_kwargs)
    model = r.attach(build_model({**FAST, **(config or {})}))
    model.build()
    status = model.run()
    return r, model, model.results(), status


@pytest.fixture(scope="module")
def trained():
    """One full-size run, shared by the tests that need a real result.

    Module-scoped because training is the expensive part of this file and the
    model is deterministic — every test here would otherwise pay for the same
    numbers again.
    """
    from tests.models.conftest import Recorder

    r = Recorder()
    model = r.attach(build_model({}))
    model.build()
    model.run()
    return r, model, model.results()


# --- shape -----------------------------------------------------------------


def test_it_runs_standalone_and_classifies_into_the_declared_classes(trained):
    r, model, rows = trained

    assert [row["class_index"] for row in rows] == list(range(len(CLASS_LABELS)))
    assert [row["class_label"] for row in rows] == list(CLASS_LABELS)
    for row in rows:
        for field in ("precision", "recall", "f1", "accuracy", "macro_f1"):
            assert math.isfinite(row[field]), f"{field} is not a number"
            assert 0.0 <= row[field] <= 1.0
        assert row["support"] >= 0
    assert sum(row["support"] for row in rows) == rows[0]["val_rows"]
    r.validate_all()


def test_the_confusion_row_agrees_with_the_per_class_counts(trained):
    """Per-class metrics and the confusion matrix must be the same numbers."""
    import json

    _, _, rows = trained
    for row in rows:
        confusion = json.loads(row["confusion_row"])
        assert set(confusion) == set(CLASS_LABELS)
        assert sum(confusion.values()) == row["support"]
        assert confusion[row["class_label"]] == row["true_positives"]
        assert row["support"] - row["true_positives"] == row["false_negatives"]


def test_it_beats_a_majority_class_baseline(trained):
    """Otherwise it is an expensive constant function.

    The classes are deliberately imbalanced (~55/30/15), which is exactly the
    shape where a headline accuracy hides a model that learned to always
    answer 'fast'.
    """
    _, model, rows = trained
    row = rows[0]

    assert row["baseline_accuracy"] == pytest.approx(model.baseline_accuracy())
    assert row["accuracy"] > row["baseline_accuracy"] + 0.03, (
        f"{row['accuracy']:.3f} vs a majority-class {row['baseline_accuracy']:.3f} — "
        "no better than a constant"
    )
    assert row["lift_over_baseline"] == pytest.approx(
        row["accuracy"] - row["baseline_accuracy"], abs=1e-6
    )
    # Balanced accuracy is the honest cross-check: a constant predictor scores
    # 1/3 on it however imbalanced the classes are.
    assert row["balanced_accuracy"] > 1.0 / len(CLASS_LABELS) + 0.1
    assert min(row["recall"] for row in rows) > 0.0, "a class was never predicted at all"


def test_it_trains_fast_enough_to_be_a_platform_test(trained):
    """This is a platform test, not a benchmark."""
    _, model, rows = trained
    assert model.train_time_seconds < 20.0
    assert rows[0]["train_time_seconds"] == pytest.approx(model.train_time_seconds, abs=1e-3)


def test_validation_accuracy_actually_improves(trained):
    _, model, _ = trained
    assert model.history[-1]["train_loss"] < model.history[0]["train_loss"], "training did nothing"
    assert model.best_accuracy >= model.history[0]["val_accuracy"]


# --- leakage ---------------------------------------------------------------


def test_the_leaking_columns_are_excluded_and_say_why(trained):
    """The trap this model exists to avoid, asserted rather than promised."""
    _, _, rows = trained

    assert set(EXCLUDED_COLUMNS) == {"duration_min", "fare_amount"}
    for column in EXCLUDED_COLUMNS:
        assert column not in FEATURE_NAMES
        assert column in rows[0]["excluded_features"]
    assert rows[0]["features"] == ",".join(FEATURE_NAMES)
    # Every feature is a transform of the one column that does not leak.
    assert all("distance" in name for name in FEATURE_NAMES)


def test_the_excluded_column_really_would_have_leaked():
    """Why duration_min is not a feature, demonstrated rather than asserted.

    With duration_min in hand the target is not predicted, it is *computed* —
    a rule with no parameters scores 100%. Any model fed it would report a
    meaningless near-perfect accuracy.
    """
    rows = nyc_taxi_trips(limit=800).rows
    distance = np.array([row["trip_distance"] for row in rows])
    duration = np.array([row["duration_min"] for row in rows])
    pace = duration / distance
    cuts = np.quantile(pace, (0.55, 0.85))

    labels = np.digitize(pace, cuts)
    cheating = np.digitize(duration / distance, cuts)  # the excluded column
    assert (cheating == labels).mean() == 1.0

    # And the honest feature set does *not* determine the label: trips at the
    # same distance land in different classes.
    bucket = np.round(distance, 1)
    ambiguous = sum(len(set(labels[bucket == b].tolist())) > 1 for b in set(bucket.tolist()))
    assert ambiguous > 0, "distance alone determines the label — the target is trivial"


def test_the_class_cuts_come_from_the_training_split_only(trained):
    """Quantiles over everything would let the validation set label itself."""
    _, model, rows = trained
    low, high = model.cuts

    assert 0 < low < high
    assert rows[0]["pace_cut_low"] == pytest.approx(low, abs=1e-6)
    assert rows[0]["pace_cut_high"] == pytest.approx(high, abs=1e-6)

    train_rows = int(model._tensors["X_train"].shape[0])
    assert train_rows == rows[0]["train_rows"]
    assert rows[0]["val_rows"] == int(model._tensors["y_val"].shape[0])


# --- progress --------------------------------------------------------------


def test_progress_is_renderable_by_a_view_that_knows_nothing_about_this_model(recorder):
    r, model, _, _ = train(recorder, {"epochs": 3})

    progress = r.of("progress")
    assert progress, "no progress at all"
    percents = [p["percent_complete"] for p in progress]
    assert percents == sorted(percents), "percent_complete went backwards"
    assert 0 < percents[0] < 100
    assert percents[-1] == 100.0
    for p in progress:
        assert p["primary_metric_label"] == "val_accuracy"
        # Bounded 0..1: a generic chart can axis this without knowing the
        # model, which an unbounded loss does not give it.
        assert 0.0 <= p["primary_metric"] <= 1.0
    r.validate_all()


def test_progress_is_two_level_and_does_not_flood(recorder):
    """Epoch and batch within epoch — the distinction this model adds."""
    r, model, _, _ = train(recorder, {"epochs": 3, "batch_updates_per_epoch": 2})

    progress = r.of("progress")
    levels = [p["payload"]["level"] for p in progress]
    assert levels.count("epoch") == 3
    assert levels.count("batch") == 3 * 2
    assert levels[-1] == "epoch", "the run should end on an epoch boundary"

    batches = progress[0]["payload"]["batches_per_epoch"]
    assert batches > 4, "too few batches for the batch level to mean anything"
    assert len(progress) < 3 * batches, "emitting per batch would flood the live path"
    for p in progress:
        payload = p["payload"]
        assert 0 <= payload["epoch"] < 3
        assert 0 <= payload["batch"] < payload["batches_per_epoch"]


def test_the_payload_carries_what_a_training_view_would_want(recorder):
    r, model, _, _ = train(recorder, {"epochs": 3})

    for p in r.of("progress"):
        payload = p["payload"]
        assert {
            "train_loss",
            "grad_norm",
            "learning_rate",
            "device",
            "val_loss",
            "macro_f1",
            "best_val_accuracy",
            "baseline_accuracy",
        } <= set(payload)
        assert math.isfinite(payload["train_loss"])
        assert payload["grad_norm"] >= 0.0
        # So a live view can badge a fallback run before any results exist.
        assert payload["data_synthetic"] is True

    rates = [p["payload"]["learning_rate"] for p in r.of("progress")]
    assert rates[-1] < rates[0], "the learning rate is scheduled, so it should move"


def test_the_device_is_reported_not_assumed(recorder):
    """This is the model that would later want a GPU. A CPU run and a GPU run
    must be distinguishable after the fact, which means the device travels in
    the telemetry and in the results — not in someone's memory of the job."""
    r, model, rows, _ = train(recorder, {"epochs": 2})

    assert str(model.device) in ("cpu", "cuda")
    assert all(p["payload"]["device"] == str(model.device) for p in r.of("progress"))
    assert rows[0]["device"] == str(model.device)
    assert any("device" in line["message"] for line in r.of("log"))
    assert rows[0]["torch_version"]


def test_a_caller_can_pin_the_device(recorder):
    _, model, rows, _ = train(recorder, {"epochs": 1, "device": "cpu"})
    assert rows[0]["device"] == "cpu"


# --- determinism -----------------------------------------------------------


def test_the_same_seed_gives_the_same_run(recorder):
    """A neural net that cannot be reproduced cannot be debugged."""
    first = train(recorder, {"epochs": 3})
    second = train(recorder, {"epochs": 3})

    assert first[1].history == second[1].history

    # Everything but the wall clock, which is not a property of the model.
    def comparable(rows):
        return [{k: v for k, v in row.items() if k != "train_time_seconds"} for row in rows]

    assert comparable(first[2]) == comparable(second[2])


def test_a_different_seed_gives_a_different_run(recorder):
    """Otherwise the determinism above would be vacuous."""
    a = train(recorder, {"epochs": 3, "seed": 1})[1]
    b = train(recorder, {"epochs": 3, "seed": 2})[1]

    assert [h["val_accuracy"] for h in a.history] != [h["val_accuracy"] for h in b.history]


# --- cancellation ----------------------------------------------------------


def test_cancelling_mid_training_still_produces_a_usable_report(recorder):
    """Cancellation is a clean outcome: the best checkpoint is kept."""
    r, model, rows, status = train(recorder, {"epochs": 200, "limit": 1200}, cancel_after=10)

    assert len(model.history) < 200, "cancellation was ignored"
    assert model.cancelled is True
    assert status == "CANCELLED"
    assert len(rows) == len(CLASS_LABELS)
    assert rows[0]["cancelled"] is True
    assert rows[0]["epochs_trained"] == len(model.history)
    assert rows[0]["epochs_planned"] == 200
    assert 0.0 <= rows[0]["accuracy"] <= 1.0
    assert all(math.isfinite(row["f1"]) for row in rows)
    assert PROVENANCE_FIELDS <= set(rows[0])
    r.validate_all()


def test_the_report_comes_from_the_best_checkpoint_not_the_last_epoch(recorder):
    _, model, rows, _ = train(recorder, {"epochs": 8})

    best = max(h["val_accuracy"] for h in model.history)
    assert model.best_accuracy == pytest.approx(best)
    assert rows[0]["accuracy"] == pytest.approx(best, abs=1e-6)


def test_cancelling_before_the_first_epoch_is_still_clean(recorder):
    """No checkpoint yet means no report — but not an exception."""
    r = recorder()
    model = r.attach(build_model(FAST))
    model.build()
    r.cancel()
    status = model.run()

    assert status == "CANCELLED"
    assert model.history == []
    assert model.results() == [], "no epoch finished, so there is nothing to report"


# --- provenance ------------------------------------------------------------


def test_the_provenance_is_logged_at_the_input_phase(trained):
    r, model, _ = trained

    input_logs = [line["message"] for line in r.of("log") if line["phase"] == "input"]
    assert any(model.data.provenance in message for message in input_logs), input_logs
    assert any("synthetic" in message for message in input_logs)
    # The leakage decision is logged too, not only documented.
    assert any("excluded" in message for message in input_logs)


def test_every_result_row_carries_the_provenance(trained):
    _, model, rows = trained

    for row in rows:
        assert PROVENANCE_FIELDS <= set(row)
        assert row["data_synthetic"] is True
        assert row["data_source"] == model.data.source
        assert row["data_rows"] == len(model.data)
        assert "no Spark session" in row["data_fallback_reason"]
    # One shape regardless of how the run went.
    assert len({tuple(sorted(row)) for row in rows}) == 1


def test_a_real_run_and_a_fallback_run_are_distinguishable(recorder, monkeypatch):
    """The whole point of carrying provenance: these two must not look alike."""
    rows_from_the_table = nyc_taxi_trips(limit=1200).rows

    def fake_loader(**_kwargs):
        return Dataset(rows=list(rows_from_the_table), source=TAXI_TRIPS_TABLE, synthetic=False)

    monkeypatch.setattr(neural_net_model, "nyc_taxi_trips", fake_loader)
    r, _, rows, _ = train(recorder, {"epochs": 2})

    assert rows[0]["data_synthetic"] is False
    assert rows[0]["data_source"] == TAXI_TRIPS_TABLE
    assert rows[0]["data_fallback_reason"] is None
    messages = [line["message"] for line in r.of("log") if line["phase"] == "input"]
    assert any(TAXI_TRIPS_TABLE in message for message in messages), messages


def test_rows_the_real_table_left_null_are_dropped_loudly(recorder, monkeypatch):
    """A real table has NULLs in it, and a NULL duration has no pace class."""
    holed = [dict(row) for row in nyc_taxi_trips(limit=1200).rows]
    holed[3]["duration_min"] = None
    holed[7]["trip_distance"] = None

    def fake_loader(**_kwargs):
        return Dataset(rows=holed, source=TAXI_TRIPS_TABLE, synthetic=False)

    monkeypatch.setattr(neural_net_model, "nyc_taxi_trips", fake_loader)
    r = recorder()
    model = r.attach(build_model({**FAST, "epochs": 1}))
    model.build()

    assert (
        int(model._tensors["X_train"].shape[0]) + int(model._tensors["X_val"].shape[0])
        == len(holed) - 2
    )


# --- the platform contract -------------------------------------------------


def test_the_harness_sees_a_build_step_and_a_results_accessor():
    from job.loader import describe_object

    handle = describe_object(build_model(), "models.neural_net")
    assert handle.build is not None and handle.run is not None
    assert handle.results is not None and handle.results_table == "results_neural_net"
    assert handle.preview_axes == ("class_index", "recall")


def test_the_results_table_ddl_matches_what_the_model_produces(trained):
    """A column the model emits and the table does not have is a write that
    fails at 3am, not a test failure."""
    _, _, rows = trained
    sql = pathlib.Path("uc_ddl/002_model_results.sql").read_text()
    block = sql.split("results_neural_net (")[1].split(")\nUSING DELTA")[0]
    columns = {
        line.strip().split()[0]
        for line in block.splitlines()
        if line.strip() and not line.strip().startswith("--")
    }

    # The harness stamps these two; the model never sees them.
    assert {"run_id", "chunk_index"} <= columns
    assert re.search(r"results_neural_net.*?COMMENT '.*'", sql, re.S)
    for row in rows:
        assert set(row) == columns - {"run_id", "chunk_index"}


def test_the_model_is_registered_with_the_heavy_extra():
    """torch is the reason per-model environments exist here. If this model
    is not in the registry it deploys with the wrong dependencies rather than
    failing."""
    import sys
    import tomllib

    root = pathlib.Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / "scripts"))
    from _registry import model_extras

    assert model_extras()["neural_net"] == "nn"
    with (root / "pyproject.toml").open("rb") as fh:
        extras = tomllib.load(fh)["project"]["optional-dependencies"]
    assert any(dep.startswith("torch") for dep in extras["nn"])


def test_the_model_imports_nothing_from_the_platform():
    """A model conforms to a contract; it does not call into the platform."""
    source = pathlib.Path("models/neural_net/model.py").read_text()
    for forbidden in ("import app", "import job", "import shared", "from shared", "from job"):
        assert forbidden not in source
