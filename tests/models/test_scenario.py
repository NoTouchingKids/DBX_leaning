"""Deterministic, fast, and numerous — the fan-out case.

The sweep is anchored on observed demand (`models._data`), so these also
assert that the baseline really comes from the data and that a run says which
data it got.
"""

from __future__ import annotations

import numpy as np
import pytest

from models._data import Dataset
from models.scenario import DEFAULT_GRID, build_model
from models.scenario import model as scenario_model


def fake_dataset(*, synthetic: bool = False, n: int = 48) -> Dataset:
    """A tiny, obviously-not-default demand curve, so a baseline drawn from it
    is unmistakably drawn from *it* and not from a constant in the model."""
    rows = [
        {
            "hour_ts": 1_600_000_000_000 + i * 3_600_000,
            "trips": 100 + 10 * i,
            "avg_fare": 20.0 + i,
            "avg_distance": 3.0,
        }
        for i in range(n)
    ]
    if synthetic:
        return Dataset(rows=rows, source="synthetic:test", synthetic=True, reason="no workspace")
    return Dataset(rows=rows, source="samples.nyctaxi.trips", synthetic=False)


@pytest.fixture
def fixed_data(monkeypatch):
    """Pin the loader so a test can reason about exact baseline numbers."""

    def loader(*, days: int = 30, seed: int = 7) -> Dataset:
        return fake_dataset()

    monkeypatch.setattr(scenario_model, "nyc_taxi_hourly", loader)
    return fake_dataset()


def test_runs_standalone_with_no_harness(recorder):
    r = recorder()
    model = r.attach(build_model())
    model.run()

    assert len(model.results()) == 72  # 6 x 4 x 3
    r.validate_all()


def test_the_same_inputs_give_the_same_outputs_every_time(recorder):
    runs = []
    for _ in range(3):
        model = recorder().attach(build_model())
        model.run()
        runs.append(model.results())
    assert runs[0] == runs[1] == runs[2]


def test_progress_does_not_flood(recorder):
    """Milliseconds per scenario: one message each would swamp the channel."""
    r = recorder()
    grid = {"a": list(range(20)), "b": list(range(10))}  # 200 scenarios
    model = r.attach(build_model({"grid": grid, "progress_every": 25}))
    model.evaluate = lambda s: {"served": 0.0, "shortfall": 0.0, "idle": 0.0, "objective": s["a"]}
    model.run()

    progress = r.of("progress")
    assert 1 <= len(progress) <= 20, f"{len(progress)} progress messages for 200 scenarios"


def test_percent_complete_is_populated_and_reaches_100(recorder):
    r = recorder()
    r.attach(build_model()).run()
    percents = [p["percent_complete"] for p in r.of("progress")]
    assert percents == sorted(percents)
    assert percents[-1] == 100.0
    assert all(0 < p <= 100 for p in percents)


def test_a_generic_progress_view_could_render_this(recorder):
    r = recorder()
    r.attach(build_model()).run()
    p = r.of("progress")[-1]
    assert p["primary_metric"] is not None and p["primary_metric_label"] == "best_objective"


def test_cancelling_mid_sweep_keeps_the_scenarios_already_done(recorder):
    r = recorder(cancel_after=4)
    model = r.attach(build_model())
    model.run()

    done = model.results()
    assert 0 < len(done) < 72, "cancellation kept everything or nothing"
    assert [row["scenario_index"] for row in done] == list(range(len(done)))


def test_the_grid_is_the_whole_scenario_space():
    model = build_model()
    assert model.total == 72
    assert len(list(model.scenarios())) == 72
    assert set(next(model.scenarios())) == set(DEFAULT_GRID)


def test_shortfall_is_penalised_and_idle_capacity_is_not_free(fixed_data):
    model = build_model()
    # The multiplier at which demand exactly meets the observed capacity.
    balanced = model.baseline.capacity / model.baseline.demand
    short = model.evaluate({"demand": 1.5 * balanced, "capacity": 1.0, "unit_cost": 1.0})
    idle = model.evaluate({"demand": 0.5 * balanced, "capacity": 1.0, "unit_cost": 1.0})
    exact = model.evaluate({"demand": balanced, "capacity": 1.0, "unit_cost": 1.0})

    assert short["shortfall"] > 0 and short["objective"] < exact["objective"]
    assert idle["idle"] > 0 and idle["objective"] < exact["objective"]
    assert exact["shortfall"] == 0 and exact["idle"] == 0


def test_the_default_sweep_actually_reaches_both_regimes(recorder):
    """A grid whose demand never crosses the observed capacity would price no
    shortfall at all, and the penalty would be dead code."""
    model = recorder().attach(build_model())
    model.run()

    rows = model.results()
    assert [r for r in rows if r["shortfall"] > 0], "no scenario is ever short"
    assert [r for r in rows if r["idle"] > 0], "no scenario ever has spare capacity"


# --- the baseline is real --------------------------------------------------


def test_the_baseline_comes_from_the_observed_data(fixed_data):
    trips = [float(row["trips"]) for row in fixed_data.rows]
    fares = [float(row["avg_fare"]) for row in fixed_data.rows]

    base = build_model().baseline

    assert base.demand == pytest.approx(float(np.mean(trips)))
    assert base.peak_demand == pytest.approx(max(trips))
    assert base.capacity == pytest.approx(float(np.percentile(trips, 90)))
    assert base.unit_cost == pytest.approx(float(np.mean(fares)))
    # Derived penalties scale with the observed unit cost, not a magic number.
    assert base.shortfall_penalty == pytest.approx(2.0 * base.unit_cost)
    assert base.idle_cost == pytest.approx(0.15 * base.unit_cost)


def test_a_different_dataset_moves_the_scenarios(monkeypatch, fixed_data, recorder):
    """If the baseline were hard-coded, doubling observed demand would change
    nothing. It has to change everything."""
    quiet = recorder().attach(build_model())
    quiet.run()

    def busier(**kwargs) -> Dataset:
        return Dataset(
            rows=[{**row, "trips": row["trips"] * 2} for row in fixed_data.rows],
            source=fixed_data.source,
            synthetic=False,
        )

    monkeypatch.setattr(scenario_model, "nyc_taxi_hourly", busier)
    loud = recorder().attach(build_model())
    loud.run()

    assert loud.baseline.demand == pytest.approx(2 * quiet.baseline.demand)
    assert loud.baseline.capacity == pytest.approx(2 * quiet.baseline.capacity)
    assert [r["demand"] for r in loud.results()] == pytest.approx(
        [2 * r["demand"] for r in quiet.results()]
    )


def test_the_scenarios_are_multipliers_on_the_baseline(fixed_data, recorder):
    model = recorder().attach(build_model())
    model.run()
    base = model.baseline

    row = model.results()[0]
    assert row["demand"] == pytest.approx(base.demand * row["demand_multiplier"])
    assert row["capacity"] == pytest.approx(base.capacity * row["capacity_multiplier"])
    assert row["unit_cost"] == pytest.approx(base.unit_cost * row["unit_cost_multiplier"])
    assert {"demand_multiplier", "capacity_multiplier", "unit_cost_multiplier"} <= set(row)


def test_a_caller_can_still_pass_its_own_grid(fixed_data, recorder):
    grid = {"demand": [1.0, 2.0], "capacity": [1.0], "unit_cost": [1.0]}
    model = recorder().attach(build_model({"grid": grid}))
    model.run()
    assert model.total == 2
    assert [row["demand_multiplier"] for row in model.results()] == [1.0, 2.0]


# --- provenance ------------------------------------------------------------


def test_provenance_is_logged_at_the_input_phase(fixed_data, recorder):
    r = recorder()
    model = r.attach(build_model())
    model.run()

    inputs = [m for m in r.of("log") if m["phase"] == "input"]
    assert any(fixed_data.provenance == m["message"] for m in inputs), inputs
    assert any("baseline demand" in m["message"] for m in inputs)
    r.validate_all()


def test_result_rows_say_which_data_they_came_from(fixed_data, recorder):
    model = recorder().attach(build_model())
    model.run()

    row = model.results()[0]
    assert row["data_source"] == "samples.nyctaxi.trips"
    assert row["data_synthetic"] is False
    assert row["data_rows"] == len(fixed_data.rows)
    assert row["data_fallback_reason"] is None
    assert row["baseline_demand"] == pytest.approx(model.baseline.demand)


def test_a_fallback_run_is_distinguishable_from_a_real_one(monkeypatch, recorder):
    monkeypatch.setattr(
        scenario_model, "nyc_taxi_hourly", lambda **kw: fake_dataset(synthetic=True)
    )
    r = recorder()
    model = r.attach(build_model())
    model.run()

    row = model.results()[0]
    assert row["data_synthetic"] is True
    assert row["data_source"] == "synthetic:test"
    assert row["data_fallback_reason"] == "no workspace"
    assert any("synthetic" in m["message"] for m in r.of("log"))
    # Same schema either way, so one results table holds both.
    real = recorder().attach(build_model())
    monkeypatch.setattr(scenario_model, "nyc_taxi_hourly", lambda **kw: fake_dataset())
    real.run()
    assert set(row) == set(real.results()[0])


def test_offline_the_default_loader_is_still_deterministic(recorder):
    """The whole model's value depends on this holding with no workspace."""
    first = build_model().baseline
    second = build_model().baseline
    assert first == second
    assert first.data_fields["data_rows"] == 30 * 24


def test_the_data_is_loaded_once_not_per_scenario(monkeypatch, recorder):
    calls = []

    def counting(**kwargs):
        calls.append(kwargs)
        return fake_dataset()

    monkeypatch.setattr(scenario_model, "nyc_taxi_hourly", counting)
    model = recorder().attach(build_model())
    model.build()
    model.run()

    assert len(calls) == 1, f"loaded the demand data {len(calls)} times"
    assert len(model.results()) == 72


def test_the_harness_sees_the_surface_it_needs():
    from job.loader import describe_object

    handle = describe_object(build_model(), "models.scenario")
    assert handle.run is not None and handle.results is not None
    assert handle.build is not None
    assert handle.results_table == "results_scenario"
    assert handle.gurobi_model is None  # a non-Gurobi model is the simpler case
