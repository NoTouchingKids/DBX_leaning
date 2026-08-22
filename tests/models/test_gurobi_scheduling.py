"""The scheduling MILP. Standalone — no harness, no transport, no Databricks."""

from __future__ import annotations

import pytest

pytest.importorskip("gurobipy", reason="needs the [gurobi] extra")

from models.gurobi_scheduling import SHIFTS, build_instance, build_model  # noqa: E402


def solved(recorder, config=None, *, callback=None):
    r = recorder()
    model = r.attach(build_model(config or {}))
    model.build()
    model.grb_model.optimize(callback) if callback else model.grb_model.optimize()
    return r, model


def test_the_instance_fits_the_restricted_licence_cap(recorder):
    """2000 variables / 2000 constraints, asserted rather than eyeballed."""
    _, model = solved(recorder)
    assert model.grb_model.NumVars <= 2000
    assert model.grb_model.NumConstrs <= 2000
    # And with real headroom, not squeaking under.
    assert model.grb_model.NumVars < 1500 and model.grb_model.NumConstrs < 1500


def test_the_model_stays_linear():
    # Quadratic terms drop the cap to 200 — see LICENCE_EXPIRY.md.
    model = build_model()
    model.build()
    assert model.grb_model.NumQConstrs == 0
    assert model.grb_model.NumQNZs == 0


def test_solves_standalone_to_a_feasible_schedule(recorder):
    r, model = solved(recorder)
    assert model.grb_model.SolCount >= 1

    rows = model.results()
    assert rows, "an optimal solve produced no assignments"
    assert {row["shift"] for row in rows} <= set(SHIFTS)
    r.validate_all()


def test_coverage_is_actually_met(recorder):
    _, model = solved(recorder)
    inst = model.instance
    staffed: dict[tuple[int, str], int] = {}
    for row in model.results():
        key = (row["day"], row["shift"])
        staffed[key] = staffed.get(key, 0) + 1

    for (day, shift), required in inst.demand.items():
        assert staffed.get((day, shift), 0) >= required, f"day {day} {shift} understaffed"


def test_nobody_works_two_shifts_in_a_day(recorder):
    _, model = solved(recorder)
    seen = set()
    for row in model.results():
        key = (row["staff"], row["day"])
        assert key not in seen, f"{row['staff']} double-booked on day {row['day']}"
        seen.add(key)


def test_nobody_works_a_morning_after_a_night(recorder):
    _, model = solved(recorder)
    assigned = {(row["staff"], row["day"], row["shift"]) for row in model.results()}
    for staff, day, shift in assigned:
        if shift == "night":
            assert (staff, day + 1, "morning") not in assigned


def test_unavailable_staff_are_never_scheduled(recorder):
    _, model = solved(recorder)
    for row in model.results():
        assert model.instance.available[(row["staff"], row["day"])]


def test_nobody_exceeds_their_shift_cap(recorder):
    _, model = solved(recorder, {"max_shifts_per_staff": 8})
    counts: dict[str, int] = {}
    for row in model.results():
        counts[row["staff"]] = counts.get(row["staff"], 0) + 1
    assert max(counts.values()) <= 8


def test_the_instance_is_deterministic_for_a_seed():
    a, b = build_instance(seed=42), build_instance(seed=42)
    assert a == b
    assert build_instance(seed=42) != build_instance(seed=43)


def test_results_are_produced_from_an_incumbent_after_an_interrupt(recorder):
    """Cancellation is a clean outcome: keep whatever incumbent exists."""
    from gurobipy import GRB

    r = recorder()
    model = r.attach(build_model({"staff_count": 20, "days": 14}))
    model.build()

    stop_after_first_solution = {"done": False}

    def callback(m, where):
        if where == GRB.Callback.MIPSOL and not stop_after_first_solution["done"]:
            stop_after_first_solution["done"] = True
            m.terminate()

    model.grb_model.optimize(callback)

    assert model.grb_model.Status == GRB.INTERRUPTED
    assert model.results(), "an interrupted solve threw away its incumbent"


def test_no_results_rather_than_a_crash_when_nothing_was_solved():
    model = build_model()
    assert model.results() == []  # never built
    model.build()
    assert model.results() == []  # built, never solved


def test_the_model_never_calls_optimize_itself():
    """The harness owns the solve so it can attach its observers. A model that
    solved itself would silently lose cancellation and progress."""
    import inspect

    from models.gurobi_scheduling import model as module

    source = inspect.getsource(module)
    assert ".optimize(" not in source


def test_the_harness_selects_the_gurobi_driver_for_this_model():
    from job.drivers import GurobiDriver, select_driver
    from job.loader import describe_object

    model = build_model()
    model.build()
    handle = describe_object(model, "models.gurobi_scheduling")

    assert handle.gurobi_model is not None
    assert handle.run is None, "a Gurobi model must not expose a blocking run()"
    assert isinstance(select_driver(handle, lambda *a, **k: None, lambda: False), GurobiDriver)
