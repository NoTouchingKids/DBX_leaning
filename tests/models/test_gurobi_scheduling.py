"""The scheduling MILP. Standalone — no harness, no transport, no Databricks."""

from __future__ import annotations

import pytest

pytest.importorskip("gurobipy", reason="needs the [gurobi] extra")

from job.models._data import Dataset  # noqa: E402
from job.models.gurobi_scheduling import (  # noqa: E402
    SHIFTS,
    build_instance,
    build_model,
    shift_of_hour,
)

#: A day-aligned epoch, so hour-of-day arithmetic in the test means what it
#: reads as. (The loader's own fallback uses an arbitrary offset — deliberately
#: not copied here, because a test that only works on aligned data would hide
#: exactly the bug that offset would cause.)
DAY_ZERO_MS = 18_519 * 86_400_000


def hourly_curve(*, days=16, by_hour=None, trips=300):
    """A synthetic `nyc_taxi_hourly`-shaped Dataset, so a test can state the
    demand curve it expects the instance to be derived from."""
    rows = []
    for d in range(days):
        for hour in range(24):
            rows.append(
                {
                    "hour_ts": DAY_ZERO_MS + (d * 24 + hour) * 3_600_000,
                    "trips": (by_hour or {}).get(hour, trips),
                    "avg_fare": 12.0,
                    "avg_distance": 2.5,
                }
            )
    return rows


def dataset(rows, *, synthetic=False, source="samples.nyctaxi.trips", reason=None):
    return Dataset(rows=rows, source=source, synthetic=synthetic, reason=reason)


def solved(recorder, config=None, *, callback=None, instance=None):
    r = recorder()
    cfg = dict(config or {})
    if instance is not None:
        cfg["instance"] = instance
    model = r.attach(build_model(cfg))
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

    from job.models.gurobi_scheduling import model as module

    source = inspect.getsource(module)
    assert ".optimize(" not in source


def test_the_harness_selects_the_gurobi_driver_for_this_model():
    from job.drivers import GurobiDriver, select_driver
    from job.loader import describe_object

    model = build_model()
    model.build()
    handle = describe_object(model, "job.models.gurobi_scheduling")

    assert handle.gurobi_model is not None
    assert handle.run is None, "a Gurobi model must not expose a blocking run()"
    assert isinstance(select_driver(handle, lambda *a, **k: None, lambda: False), GurobiDriver)


# --- the demand curve is real, and says so ---------------------------------


def test_the_shift_buckets_partition_the_day():
    """Every hour lands in exactly one shift, and the three are equal length —
    otherwise a bucket's trip volume is not comparable with its neighbours'."""
    counts: dict[str, int] = {}
    for hour in range(24):
        shift = shift_of_hour(hour)
        counts[shift] = counts.get(shift, 0) + 1
    assert counts == {"morning": 8, "evening": 8, "night": 8}


def test_demand_follows_the_hourly_trip_volumes():
    """The whole point of the change: coverage tracks the demand curve. A
    morning-heavy curve must produce morning-heavy staffing."""
    morning_heavy = hourly_curve(by_hour={h: 3000 for h in range(6, 14)}, trips=200)
    inst = build_instance(demand_data=dataset(morning_heavy))

    for d in range(inst.days):
        assert inst.demand[(d, "morning")] > inst.demand[(d, "evening")]
        assert inst.demand[(d, "morning")] > inst.demand[(d, "night")]

    # And the other way round, from the same generator — so the assertion above
    # is about the data, not about "morning" being special.
    night_heavy = hourly_curve(
        by_hour={h: 3000 for h in list(range(22, 24)) + list(range(0, 6))}, trips=200
    )
    flipped = build_instance(demand_data=dataset(night_heavy))
    for d in range(flipped.days):
        assert flipped.demand[(d, "night")] > flipped.demand[(d, "morning")]


def test_the_curve_varies_across_days_not_just_across_shifts():
    """A weekly cycle in the data has to survive into the requirement, or the
    coverage constraints are a flat number wearing a costume."""
    quiet_days = {3, 4}
    rows = []
    for d in range(16):
        volume = 100 if d % 7 in quiet_days else 1200
        rows.extend(hourly_curve(days=1, trips=volume))
        for row in rows[-24:]:
            row["hour_ts"] += d * 24 * 3_600_000
    inst = build_instance(demand_data=dataset(rows))
    totals = {d: sum(inst.demand[(d, s)] for s in SHIFTS) for d in range(inst.days)}
    assert len(set(totals.values())) > 1, "demand is flat despite a varying curve"


def test_an_explicit_trips_per_staff_ratio_is_used_as_given():
    rows = hourly_curve(trips=100)  # 800 trips per 8-hour bucket
    inst = build_instance(demand_data=dataset(rows), trips_per_staff=400.0)
    assert inst.demand_meta["demand_trips_per_staff"] == 400.0
    assert inst.demand[(0, "morning")] == 2  # 800 / 400


def test_a_partial_first_day_is_not_read_as_a_quiet_one():
    """Real windows start mid-day. A half-day of trips must not become a
    half-staffed shift."""
    rows = hourly_curve(trips=800)
    rows = [r for r in rows if r["hour_ts"] >= DAY_ZERO_MS + 13 * 3_600_000]
    inst = build_instance(demand_data=dataset(rows))
    totals = {sum(inst.demand[(d, s)] for s in SHIFTS) for d in range(inst.days)}
    assert len(totals) == 1, "a partial calendar day leaked into the curve"


def test_a_curve_that_cannot_be_bucketed_falls_back_to_flat_demand():
    inst = build_instance(demand_data=dataset([], synthetic=True, reason="empty"))
    assert inst.demand_meta["demand_derived_from"] == "flat_demand_per_shift"
    assert {inst.demand[(d, "morning")] for d in range(inst.days)} == {4}


def test_build_instance_runs_fully_synthetically_without_reading_anything(monkeypatch):
    from job.models.gurobi_scheduling import instance as instance_module

    def explode(**_):  # pragma: no cover - the point is that it is not called
        raise AssertionError("build_instance read sample data when told not to")

    monkeypatch.setattr(instance_module, "nyc_taxi_hourly", explode)
    inst = build_instance(use_sample_data=False)
    assert inst.demand_meta["demand_derived_from"] == "flat_demand_per_shift"
    assert inst.data_meta["data_synthetic"] is True
    assert inst.total_demand == 9 * 14


def test_real_demand_is_scaled_down_rather_than_the_model_grown(recorder):
    """The licence cap is not negotiable. A curve implying a bigger workforce
    clips the requirement; it never adds staff, days or variables."""
    rows = hourly_curve(trips=5000)
    inst = build_instance(demand_data=dataset(rows), trips_per_staff=1.0)

    assert inst.demand_meta["demand_clipped_to_capacity"] is True
    for d in range(inst.days):
        available = sum(inst.available[(s, d)] for s in inst.staff)
        assert sum(inst.demand[(d, s)] for s in SHIFTS) <= available
    assert inst.total_demand <= len(inst.staff) * inst.max_shifts_per_staff

    # Still the same model, and still solvable — clipping has to produce a
    # feasible instance, not merely a smaller number.
    _, model = solved(recorder, instance=inst)
    assert model.grb_model.NumVars == 840
    assert model.grb_model.NumConstrs == 602
    assert model.grb_model.SolCount >= 1


def test_real_demand_does_not_change_the_model_size():
    from_data = build_model({})
    from_data.build()
    flat = build_model({"use_sample_data": False})
    flat.build()
    assert (from_data.grb_model.NumVars, from_data.grb_model.NumConstrs) == (
        flat.grb_model.NumVars,
        flat.grb_model.NumConstrs,
    )


def test_the_provenance_is_logged_at_the_input_phase(recorder):
    r = recorder()
    model = r.attach(build_model({}))
    model.build()

    inputs = [m for m in r.of("log") if m.get("phase") == "input"]
    assert inputs, "nothing was logged about where the demand came from"
    assert any(model.instance.data_meta["data_source"] in m["message"] for m in inputs)
    r.validate_all()


def test_every_result_row_says_where_the_demand_came_from(recorder):
    _, model = solved(recorder)
    described = model.instance.data_meta
    rows = model.results()
    assert rows
    for row in rows:
        assert row["data_source"] == described["data_source"]
        assert row["data_synthetic"] == described["data_synthetic"]
        assert row["data_rows"] == described["data_rows"]
        assert "data_fallback_reason" in row


def test_a_real_run_and_a_fallback_run_are_distinguishable_afterwards(recorder):
    rows = hourly_curve(trips=900)
    real = build_instance(demand_data=dataset(rows))
    fell_back = build_instance(
        demand_data=dataset(
            rows, synthetic=True, source="synthetic:hourly-demand", reason="no Spark session"
        )
    )

    _, real_model = solved(recorder, instance=real)
    _, fallback_model = solved(recorder, instance=fell_back)

    assert real_model.results()[0]["data_synthetic"] is False
    assert real_model.results()[0]["data_source"] == "samples.nyctaxi.trips"
    assert real_model.results()[0]["data_fallback_reason"] is None

    assert fallback_model.results()[0]["data_synthetic"] is True
    assert fallback_model.results()[0]["data_fallback_reason"] == "no Spark session"


def test_a_data_derived_instance_still_fits_the_licence_cap(recorder):
    """The cap holds on whatever the curve says, not just on the default one."""
    for volume in (1, 50, 5_000, 500_000):
        inst = build_instance(demand_data=dataset(hourly_curve(trips=volume)))
        _, model = solved(recorder, instance=inst)
        assert model.grb_model.NumVars <= 2000
        assert model.grb_model.NumConstrs <= 2000
        assert model.grb_model.SolCount >= 1, f"infeasible at {volume} trips/hour"
