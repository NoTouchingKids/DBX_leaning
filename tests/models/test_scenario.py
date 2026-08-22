"""Deterministic, fast, and numerous — the fan-out case."""

from __future__ import annotations

from models.scenario import DEFAULT_GRID, build_model


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


def test_shortfall_is_penalised_and_idle_capacity_is_not_free():
    model = build_model()
    short = model.evaluate({"demand": 1.5, "capacity": 1.0, "unit_cost": 10.0})
    idle = model.evaluate({"demand": 0.5, "capacity": 1.0, "unit_cost": 10.0})
    exact = model.evaluate({"demand": 1.0, "capacity": 1.0, "unit_cost": 10.0})

    assert short["shortfall"] == 0.5 and short["objective"] < exact["objective"]
    assert idle["idle"] == 0.5 and idle["objective"] < exact["objective"]


def test_the_harness_sees_the_surface_it_needs():
    from job.loader import describe_object

    handle = describe_object(build_model(), "models.scenario")
    assert handle.run is not None and handle.results is not None
    assert handle.results_table == "results_scenario"
    assert handle.gurobi_model is None  # a non-Gurobi model is the simpler case
