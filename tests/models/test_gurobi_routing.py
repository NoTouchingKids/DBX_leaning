"""The routing MILP. Standalone — no transport, no Databricks.

Two things separate this suite from `test_gurobi_scheduling.py`. First, the
model is *wrong without its callback*: the formulation has no constraint
forcing the selected edges to form depot-connected routes, so a solve with the
callback removed returns subtours, and there is a test that says exactly that.
Second, because the callback matters, this suite runs the model through the
real `job/drivers/gurobi.py` and asserts that the harness's observers and the
model's own cut separation both ran in Gurobi's single callback slot — which
until this model existed had only ever been exercised against a stub.
"""

from __future__ import annotations

import math

import pytest

pytest.importorskip("gurobipy", reason="needs the [gurobi] extra")

from models._data import Dataset  # noqa: E402
from models.gurobi_routing import (  # noqa: E402
    MAX_STOPS,
    Instance,
    Stop,
    build_instance,
    build_model,
    variable_count_for,
)
from models.gurobi_routing.instance import (  # noqa: E402
    MAX_RADIUS,
    MAX_SERVICE_MINUTES,
)

#: The bundled restricted licence. Not a style preference — past it, the solve
#: fails outright. See models/gurobi_scheduling/LICENCE_EXPIRY.md.
LICENCE_VARS = 2000
LICENCE_CONSTRS = 2000


# --- fixtures and helpers ---------------------------------------------------


def recorder_class():
    from tests.models.conftest import Recorder

    return Recorder


def trip_rows(n=40, *, distance=2.0, fare=6.0, duration=10.0, overrides=None):
    """`nyc_taxi_trips`-shaped rows, so a test can state the trips it expects
    the geometry to be derived from."""
    rows = [
        {"trip_distance": distance, "fare_amount": fare, "duration_min": duration} for _ in range(n)
    ]
    for index, patch in (overrides or {}).items():
        rows[index].update(patch)
    return rows


def dataset(rows, *, synthetic=False, source="samples.nyctaxi.trips", reason=None):
    return Dataset(rows=rows, source=source, synthetic=synthetic, reason=reason)


def solve(config=None, *, callback="own"):
    """Build and solve one model the way the harness would, minus the harness.

    `callback="own"` installs the model's own callback only — enough for the
    formulation to be correct. `callback=None` installs nothing, which is how
    a test shows what the callback is actually holding up.
    """
    r = recorder_class()()
    model = r.attach(build_model(dict(config or {})))
    model.build()
    model.grb_model.optimize(model.gurobi_callback if callback == "own" else None)
    return r, model


def components(model):
    """Connected components of the selected stop-to-stop edges in whatever
    solution the model holds, computed here rather than via `routes()` so the
    subtour assertions do not depend on the code that reconstructs routes."""
    n = model.instance.stop_count
    adjacency = {i: [] for i in range(1, n + 1)}
    for (i, j), var in model._edge.items():
        if var.X > 0.5:
            adjacency[i].append(j)
            adjacency[j].append(i)

    seen, found = set(), []
    for start in range(1, n + 1):
        if start in seen:
            continue
        stack, component = [start], []
        seen.add(start)
        while stack:
            node = stack.pop()
            component.append(node)
            for neighbour in adjacency[node]:
                if neighbour not in seen:
                    seen.add(neighbour)
                    stack.append(neighbour)
        found.append(sorted(component))
    return found


def depot_attached(model):
    return {j for j, var in model._depot_edge.items() if var.X > 0.5}


@pytest.fixture(scope="module")
def default_solve():
    """The default instance, solved once. Every read-only assertion about a
    good solution shares it rather than paying for the solve again."""
    return solve()


@pytest.fixture(scope="module")
def driven():
    """The same model, solved by the real harness driver.

    This is the composition under test: the driver installs *its* callback
    (log capture, progress sampling, cancellation) and calls the model's from
    inside it, because Gurobi has exactly one callback slot.
    """
    from job.drivers.gurobi import GurobiDriver
    from job.loader import describe_object

    r = recorder_class()()
    model = r.attach(build_model({}))
    model.build()
    handle = describe_object(model, "models.gurobi_routing")
    handle.refresh()
    # progress_every_s is deliberately small: the default 2s cadence is right
    # for a real run and would sample nothing at all from a one-second solve.
    driver = GurobiDriver(handle, r.emit, r.should_cancel, progress_every_s=0.05)
    result = driver.run()
    return r, model, result


# --- the licence cap --------------------------------------------------------


def test_the_instance_fits_the_restricted_licence_cap(default_solve):
    """2000 variables / 2000 constraints, asserted rather than eyeballed."""
    _, model = default_solve
    assert model.grb_model.NumVars <= LICENCE_VARS
    assert model.grb_model.NumConstrs <= LICENCE_CONSTRS
    # And with real headroom, not squeaking under.
    assert model.grb_model.NumVars < 1500 and model.grb_model.NumConstrs < 1500


def test_the_model_stays_linear(default_solve):
    # Quadratic terms drop the cap to 200 — see LICENCE_EXPIRY.md.
    _, model = default_solve
    assert model.grb_model.NumQConstrs == 0
    assert model.grb_model.NumQNZs == 0


def test_the_largest_allowed_instance_still_fits_the_cap():
    """An edge formulation grows quadratically, so the cap is a size limit on
    the *instance*, not a thing to check once on the default one."""
    assert variable_count_for(MAX_STOPS) <= LICENCE_VARS
    model = build_model(
        {"instance": build_instance(stop_count=MAX_STOPS, vehicles=5, use_sample_data=False)}
    )
    model.build()  # built, not solved: this is about size, not about answers
    assert model.grb_model.NumVars <= LICENCE_VARS
    assert model.grb_model.NumConstrs <= LICENCE_CONSTRS


def test_the_generator_refuses_a_size_the_licence_cannot_hold():
    """The cap is not negotiable, and this is the one knob that has to refuse
    rather than quietly clip: a bigger instance is a different problem."""
    with pytest.raises(ValueError, match="2000"):
        build_instance(stop_count=MAX_STOPS + 1, use_sample_data=False)


def test_lazy_cuts_never_grow_the_constraint_count(default_solve):
    """Why a routing model fits here at all: separated constraints live in
    Gurobi's lazy pool, so hundreds of them cost nothing against the cap."""
    _, model = default_solve
    assert model.cuts_added > 0, "no cuts were separated; the rest of this says nothing"
    assert model.grb_model.NumConstrs == model.instance.stop_count + 1


# --- the formulation, and what the callback holds up ------------------------


def test_solves_standalone_to_a_feasible_routing(default_solve):
    r, model = default_solve
    assert model.grb_model.SolCount >= 1

    routes = model.routes()
    assert len(routes) == model.instance.vehicles
    visited = [stop for route in routes for stop in route]
    assert sorted(visited) == list(range(1, model.instance.stop_count + 1)), (
        "every stop is served exactly once"
    )
    r.validate_all()


def test_every_route_is_connected_to_the_depot(default_solve):
    """The property the lazy constraints exist to enforce."""
    _, model = default_solve
    attached = depot_attached(model)
    for component in components(model):
        assert attached & set(component), f"component {component} is a subtour"


def test_no_route_exceeds_the_vehicle_capacity(default_solve):
    _, model = default_solve
    inst = model.instance
    for route in model.routes():
        load = sum(inst.demand(stop) for stop in route)
        assert load <= inst.capacity_minutes + 1e-6


def test_without_the_lazy_callback_the_solution_contains_subtours():
    """The honest reason this model needs its own callback.

    Nothing in the formulation says the selected edges must reach the depot —
    those constraints number 2^n and are separated, not written down. Solve
    without separating them and the optimiser happily returns disconnected
    cycles. If this test ever stops finding one, the callback has stopped
    being load-bearing and this model has stopped earning its place.
    """
    _, model = solve(callback=None)
    attached = depot_attached(model)
    orphans = [c for c in components(model) if not attached & set(c)]
    assert orphans, "expected subtours with no cuts separated"


def test_a_component_needing_two_vehicles_is_cut_even_though_it_reaches_the_depot():
    """The capacity half of the cut family, exercised directly.

    A connected chain of stops is a legal *shape*; it is illegal when its
    service time needs more vehicles than the one chain implies. Same
    inequality, `k(S) = 2` instead of 1 — which is why one family of cuts does
    both jobs and there is no second callback.
    """
    stops = tuple(Stop(f"stop-{i}", float(i), 0.0, 10.0) for i in range(4))
    tight = Instance(stops=stops, vehicles=2, capacity_minutes=15.0, cost_per_distance=1.0)
    model = build_model({"instance": tight})

    # A chain 1-2-3: three stops, 30 minutes of work, a 15-minute vehicle.
    chain = {(1, 2): 1.0, (2, 3): 1.0, (1, 3): 0.0, (1, 4): 0.0, (2, 4): 0.0, (3, 4): 0.0}
    assert model._violated_cut([1, 2, 3], chain) == ([1, 2, 3], 2)

    roomy = Instance(stops=stops, vehicles=2, capacity_minutes=100.0, cost_per_distance=1.0)
    assert build_model({"instance": roomy})._violated_cut([1, 2, 3], chain) is None


def test_result_rows_reconstruct_the_routes_in_visit_order(default_solve):
    _, model = default_solve
    rows = model.results()
    assert len(rows) == model.instance.stop_count

    by_route: dict[int, list[dict]] = {}
    for row in rows:
        by_route.setdefault(row["route"], []).append(row)

    for route_rows in by_route.values():
        assert [r["visit_order"] for r in route_rows] == list(range(1, len(route_rows) + 1))
        assert route_rows[0]["previous_stop"] == "depot"
        for previous, row in zip(route_rows[:-1], route_rows[1:], strict=True):
            assert row["previous_stop"] == previous["stop"]
        assert route_rows[0]["route_stops"] == len(route_rows)


def test_the_reported_legs_are_the_distances_the_objective_paid_for(default_solve):
    _, model = default_solve
    inst = model.instance
    at = {stop.name: (stop.x, stop.y) for stop in inst.stops}
    at["depot"] = inst.depot

    for row in model.results():
        here, before = at[row["stop"]], at[row["previous_stop"]]
        expected = math.hypot(here[0] - before[0], here[1] - before[1])
        assert row["leg_distance"] == pytest.approx(expected, abs=1e-6)
        assert row["leg_cost"] == pytest.approx(expected * inst.cost_per_distance, abs=1e-6)
        assert row["distance_to_depot"] == pytest.approx(math.hypot(*here), abs=1e-6)


def test_no_results_rather_than_a_crash_when_nothing_was_solved():
    model = build_model({"use_sample_data": False, "stop_count": 8, "vehicles": 2})
    assert model.results() == []  # never built
    assert model.routes() == []
    model.build()
    assert model.results() == []  # built, never solved


# --- the harness contract ---------------------------------------------------


def test_the_model_never_calls_optimize_itself():
    """The harness owns the solve so it can attach its observers. A model that
    solved itself would silently lose cancellation and progress — and here it
    would also take the callback slot its own cuts need."""
    import inspect

    from models.gurobi_routing import instance as instance_module
    from models.gurobi_routing import model as model_module

    for module in (model_module, instance_module):
        assert ".optimize(" not in inspect.getsource(module)


def test_the_harness_discovers_our_callback_and_selects_the_gurobi_driver():
    from job.drivers import GurobiDriver, select_driver
    from job.loader import describe_object

    model = build_model({"use_sample_data": False, "stop_count": 8, "vehicles": 2})
    model.build()
    handle = describe_object(model, "models.gurobi_routing")

    assert handle.gurobi_model is not None
    assert handle.run is None, "a Gurobi model must not expose a blocking run()"
    # The name matters: discovered under a private name, the cuts would never
    # be separated and the solver would return subtours.
    assert handle.found["model_callback"] == "gurobi_callback"
    assert handle.model_callback.__func__ is type(model).gurobi_callback
    assert isinstance(select_driver(handle, lambda *a, **k: None, lambda: False), GurobiDriver)


def test_the_harness_composes_our_callback_with_its_own_observers(driven):
    """The point of this model.

    Gurobi allows one callback. Both sides need it. Assert that both actually
    ran in the same solve: the model separated cuts, and the harness captured
    solver log lines and sampled MIP progress.
    """
    r, model, result = driven
    from shared.envelope import RunStatus

    assert result.status is RunStatus.SUCCEEDED

    # ours
    assert model.cuts_added > 0
    assert model.separation_calls > 0
    # and the property those cuts buy, in a solve we did not drive ourselves
    attached = depot_attached(model)
    assert all(attached & set(c) for c in components(model))

    # theirs
    solver_logs = [m for m in r.of("log") if m.get("source") == "gurobi"]
    assert solver_logs, "the harness captured no solver output"
    assert all("\n" not in m["message"] for m in solver_logs), (
        "MESSAGE fires on chunks, not lines; a newline here is a split that did not happen"
    )
    progress = r.of("progress")
    assert progress, "the harness sampled no progress"
    assert {p["primary_metric_label"] for p in progress} == {"mip_gap"}
    assert set(progress[0]["payload"]) == {
        "best_bound",
        "incumbent",
        "nodes_explored",
        "nodes_remaining",
        "solution_count",
    }

    # ours again, on the same channel — the model's own logs are not lost by
    # the composition either.
    assert [m for m in r.of("log") if m.get("source") == "model"]
    assert model.results()
    r.validate_all()


def test_the_pre_incumbent_sentinel_never_reaches_a_progress_message(driven):
    """Gurobi reports ±1e100 for the incumbent before the first feasible
    solution. It is finite, so no NaN guard catches it, and raw it poisons a
    chart's axis."""
    r, _, _ = driven
    progress = r.of("progress")
    assert progress

    for sample in progress:
        for key in ("incumbent", "best_bound"):
            value = sample["payload"][key]
            assert value is None or abs(value) < 1e100, f"raw sentinel in {key}"
        if sample["payload"]["solution_count"] == 0:
            assert sample["payload"]["incumbent"] is None, (
                "an incumbent was reported before one existed"
            )
        metric = sample["primary_metric"]
        assert metric is None or 0.0 <= metric < 1e100


def test_results_are_produced_from_an_incumbent_after_a_cancellation():
    """A user-requested stop is a clean outcome, not an error: terminate(),
    `optimize()` returns INTERRUPTED, and whatever incumbent exists is still a
    result — a real set of routes, because the cuts made it one."""
    from gurobipy import GRB

    from job.drivers.gurobi import GurobiDriver
    from job.loader import describe_object
    from shared.envelope import RunStatus

    r = recorder_class()()
    # Deliberately slower than the default: cancellation has to land while the
    # solve is still running. time_limit_s bounds the test if it never does.
    model = r.attach(build_model({"stop_count": 30, "vehicles": 3, "time_limit_s": 45}))
    model.build()

    def cancel_once_an_incumbent_exists():
        return any(m["payload"]["incumbent"] is not None for m in r.of("progress"))

    handle = describe_object(model, "models.gurobi_routing")
    handle.refresh()
    result = GurobiDriver(
        handle, r.emit, cancel_once_an_incumbent_exists, progress_every_s=0.05
    ).run()

    assert model.grb_model.Status == GRB.INTERRUPTED, "the solve was not interrupted"
    assert result.status is RunStatus.CANCELLED
    assert model.grb_model.SolCount >= 1

    rows = model.results()
    assert rows, "a cancelled run threw away its incumbent"
    assert len(rows) == model.instance.stop_count
    attached = depot_attached(model)
    assert all(attached & set(c) for c in components(model)), (
        "the incumbent kept from a cancelled solve contains subtours"
    )
    r.validate_all()


# --- the stops are real, and say so -----------------------------------------


def test_the_instance_is_deterministic_for_a_seed():
    data = dataset(trip_rows(60, distance=3.0))
    assert build_instance(seed=42, trip_data=data) == build_instance(seed=42, trip_data=data)
    assert build_instance(seed=42, trip_data=data) != build_instance(seed=43, trip_data=data)


def test_stops_take_their_radius_and_service_time_from_real_trips():
    """The signal the data actually carries: how far out a stop is, and how
    long it takes. (The bearing is generated — the taxi sample has no
    coordinates, and pretending otherwise would dress a random number up as
    data.)"""
    rows = trip_rows(30, distance=2.0, duration=9.0)
    rows[0].update({"trip_distance": 7.5, "duration_min": 21.0})
    inst = build_instance(stop_count=12, trip_data=dataset(rows))

    assert math.hypot(inst.stops[0].x, inst.stops[0].y) == pytest.approx(7.5, abs=1e-4)
    assert inst.stops[0].service_minutes == pytest.approx(21.0)
    for stop in inst.stops[1:]:
        assert math.hypot(stop.x, stop.y) == pytest.approx(2.0, abs=1e-4)
        assert stop.service_minutes == pytest.approx(9.0)
    assert inst.routing_meta["stops_derived_from"] == "trip_distance_and_duration"


def test_the_price_of_distance_is_the_median_observed_fare_per_mile():
    """A median, not a mean: short trips carry a fixed flag-fall, and their
    per-mile rate is a number about the flag-fall, not about distance."""
    rows = trip_rows(21, distance=1.0, fare=4.0)
    rows[0]["fare_amount"] = 400.0  # one absurd trip must not set the price
    inst = build_instance(stop_count=12, trip_data=dataset(rows))
    assert inst.cost_per_distance == pytest.approx(4.0)


def test_outlying_trips_are_clamped_rather_than_dominating_the_geometry():
    """One 40-mile airport run among 2-mile hops turns a routing problem into
    a star, and one 3-hour stop silently claims a vehicle of its own."""
    rows = trip_rows(30, distance=2.0, duration=9.0)
    rows[0].update({"trip_distance": 40.0, "duration_min": 300.0})
    inst = build_instance(stop_count=12, trip_data=dataset(rows))

    assert math.hypot(inst.stops[0].x, inst.stops[0].y) == pytest.approx(MAX_RADIUS, abs=1e-4)
    assert inst.stops[0].service_minutes == MAX_SERVICE_MINUTES


def test_a_trip_with_a_null_fare_drops_the_row_rather_than_raising():
    """A real AVG-free column can still be NULL, and that only ever shows up
    on a workspace — the worst shape a bug can have."""
    rows = trip_rows(30, distance=2.0)
    rows[3]["fare_amount"] = None
    inst = build_instance(stop_count=12, trip_data=dataset(rows))
    assert inst.data_meta["data_rows"] == 29
    assert inst.routing_meta["stops_derived_from"] == "trip_distance_and_duration"


def test_too_few_usable_trips_falls_back_to_generated_stops():
    inst = build_instance(stop_count=12, trip_data=dataset(trip_rows(5)))
    assert inst.routing_meta["stops_derived_from"] == "generated_radii"
    assert inst.data_meta["data_synthetic"] is True
    assert "fewer than 12" in inst.data_meta["data_fallback_reason"]
    assert len(inst.stops) == 12


def test_build_instance_runs_fully_synthetically_without_reading_anything(monkeypatch):
    from models.gurobi_routing import instance as instance_module

    def explode(**_):  # pragma: no cover - the point is that it is not called
        raise AssertionError("build_instance read sample data when told not to")

    monkeypatch.setattr(instance_module, "nyc_taxi_trips", explode)
    inst = build_instance(stop_count=10, vehicles=2, use_sample_data=False)
    assert inst.routing_meta["stops_derived_from"] == "generated_radii"
    assert inst.data_meta["data_synthetic"] is True
    assert inst.data_meta["data_fallback_reason"] == "sample data not requested"


def test_the_vehicles_can_always_carry_the_work():
    """Capacity is fitted to the fleet that exists. A data-derived instance
    that needed a fourth vehicle would be infeasible for a reason that has
    nothing to do with routing."""
    for distance in (0.2, 2.0, 40.0):
        inst = build_instance(
            stop_count=20, vehicles=3, trip_data=dataset(trip_rows(40, distance=distance))
        )
        assert inst.vehicles * inst.capacity_minutes >= inst.total_service_minutes
        assert inst.routing_meta["minimum_vehicles"] <= inst.vehicles


def test_the_provenance_is_logged_at_the_input_phase(default_solve):
    r, model = default_solve
    inputs = [m for m in r.of("log") if m.get("phase") == "input"]
    assert inputs, "nothing was logged about where the stops came from"
    assert any(model.instance.data_meta["data_source"] in m["message"] for m in inputs)


def test_every_result_row_says_where_the_stops_came_from(default_solve):
    _, model = default_solve
    described = model.instance.data_meta
    rows = model.results()
    assert rows
    for row in rows:
        assert row["data_source"] == described["data_source"]
        assert row["data_synthetic"] == described["data_synthetic"]
        assert row["data_rows"] == described["data_rows"]
        assert "data_fallback_reason" in row


def test_a_real_run_and_a_fallback_run_are_distinguishable_afterwards():
    rows = trip_rows(40, distance=2.5, duration=8.0)
    real = build_instance(stop_count=10, vehicles=2, trip_data=dataset(rows))
    fell_back = build_instance(
        stop_count=10,
        vehicles=2,
        trip_data=dataset(
            rows, synthetic=True, source="synthetic:trips", reason="no Spark session"
        ),
    )

    _, real_model = solve({"instance": real})
    _, fallback_model = solve({"instance": fell_back})

    assert real_model.results()[0]["data_synthetic"] is False
    assert real_model.results()[0]["data_source"] == "samples.nyctaxi.trips"
    assert real_model.results()[0]["data_fallback_reason"] is None

    assert fallback_model.results()[0]["data_synthetic"] is True
    assert fallback_model.results()[0]["data_fallback_reason"] == "no Spark session"


# --- registration -----------------------------------------------------------


def test_the_results_table_ddl_matches_what_the_model_produces(default_solve):
    """A column the model emits and the table does not have is a write that
    fails at 3am, not a test failure."""
    import pathlib
    import re

    _, model = default_solve
    sql = pathlib.Path("uc_ddl/002_model_results.sql").read_text()
    block = sql.split("results_gurobi_routing (")[1].split(")\nUSING DELTA")[0]
    columns = {
        line.strip().split()[0]
        for line in block.splitlines()
        if line.strip() and not line.strip().startswith("--")
    }

    # The harness stamps these two; the model never sees them.
    assert {"run_id", "chunk_index"} <= columns
    assert re.search(r"results_gurobi_routing.*?COMMENT '.*'", sql, re.S)
    assert model.results_table == "results_gurobi_routing"
    for row in model.results():
        assert set(row) == columns - {"run_id", "chunk_index"}


def test_the_model_is_registered_against_the_existing_gurobi_extra():
    """Two Gurobi jobs, one gurobipy pin, one bundled-licence expiry date. A
    model missing from the registry deploys with the wrong dependencies rather
    than failing."""
    import pathlib
    import sys

    root = pathlib.Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / "scripts"))
    from _registry import model_extras

    assert model_extras()["gurobi_routing"] == "gurobi"
    assert model_extras()["gurobi_scheduling"] == "gurobi"
