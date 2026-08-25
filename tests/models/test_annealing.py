"""The zero-dependency control case.

Two things are being defended here that no other model's suite defends:

1. **The package imports nothing outside the standard library.** That is the
   entire reason this model is in the lineup — it is the evidence that the
   microservice split really can produce a minimal job environment. A test,
   not a comment, because an `import numpy` added in a hurry is invisible in
   review and fatal to the claim.
2. **A stochastic search is reproducible, and is better than chance.** Same
   seed, same answer, every time — otherwise it cannot be debugged. And it
   has to beat a random baseline — otherwise it is an expensive random number
   generator, and the suite should say so rather than assert it ran.
"""

from __future__ import annotations

import ast
import math
import pathlib
import random
import sys

import pytest

from job.models._data import Dataset
from job.models.annealing import build_model
from job.models.annealing import model as annealing_model

PACKAGE = pathlib.Path(annealing_model.__file__).parent

#: Small enough to keep the suite quick, large enough that the search is not
#: trivially exhaustive.
FAST = {"iterations": 4_000, "n_items": 80, "baseline_trials": 40}


def fake_dataset(*, synthetic: bool = False, n: int = 60) -> Dataset:
    """A tiny, obviously-not-default trip table, so a knapsack built from it
    is unmistakably built from *it* and not from constants in the model."""
    rows = [
        {
            "trip_distance": 1.0 + 0.1 * i,
            "fare_amount": 5.0 + (i % 7) * 3.0,
            "duration_min": 4.0 + (i % 5) * 2.0,
        }
        for i in range(n)
    ]
    if synthetic:
        return Dataset(rows=rows, source="synthetic:test", synthetic=True, reason="no workspace")
    return Dataset(rows=rows, source="samples.nyctaxi.trips", synthetic=False)


@pytest.fixture
def fixed_data(monkeypatch):
    """Pin the loader so a test can reason about exact numbers."""

    def loader(*, limit: int = 2000, seed: int = 11) -> Dataset:
        return fake_dataset()

    monkeypatch.setattr(annealing_model, "nyc_taxi_trips", loader)
    return fake_dataset()


# --- it runs, with nothing but two callables -------------------------------


def test_runs_standalone_with_no_harness(recorder):
    r = recorder()
    model = r.attach(build_model(FAST))
    model.run()

    rows = model.results()
    assert rows, "the search chose nothing at all"
    assert rows[0]["objective"] > 0
    r.validate_all()


def test_the_harness_sees_the_surface_it_needs():
    from job.loader import describe_object

    handle = describe_object(build_model(), "job.models.annealing")
    assert handle.run is not None and handle.results is not None
    assert handle.build is not None
    assert handle.results_table == "results_annealing"
    assert handle.preview_axes == ("rank", "value_density")
    assert handle.gurobi_model is None  # a plain-Python model is the simple case


# --- the whole point: no third-party dependencies --------------------------


def _imported_top_level_modules() -> set[str]:
    """Every top-level module name imported anywhere in the package."""
    names: set[str] = set()
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # a relative import stays inside the package
                    continue
                if node.module:
                    names.add(node.module.split(".")[0])
    return names


def test_the_package_imports_nothing_outside_the_standard_library():
    """This model's reason to exist. If it ever fails, either the offending
    import goes, or the model does — do not relax the assertion."""
    #: `models._data` is the one non-stdlib name allowed, and it is stdlib-only
    #: itself, so the deployed environment still carries no third-party
    #: package. That makes the claim one about *this* package's dependencies,
    #: not about zero imports.
    allowed = sys.stdlib_module_names | {"models", "__future__"}
    offenders = sorted(_imported_top_level_modules() - allowed)
    assert not offenders, f"job/models/annealing imports third-party packages: {offenders}"


def test_the_only_non_stdlib_import_is_the_shared_data_loader():
    """`_data` is reached RELATIVELY — `from .._data import ...`.

    That is not style. An absolute `from job.models._data import ...` would be
    a model importing the platform by name, which the test above forbids
    outright, and it would stop resolving the moment this package is built
    into its own wheel (`scripts/build_model_wheel.py` stages it under a bare
    `models/`).
    """
    # level 1 is this package's own modules (`from .model import ...`);
    # level 2 is its parent, `job/models/`, where `_data` lives.
    outward = {
        node.module
        for path in sorted(PACKAGE.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path)))
        if isinstance(node, ast.ImportFrom) and node.level > 1
    }
    assert outward == {"_data"}, outward


def test_the_registered_extra_is_empty():
    """An empty extra is the deployable form of the claim above. If the
    tooling had rejected one, this model would need saying so out loud rather
    than quietly gaining a package."""
    import tomllib

    # model.py -> annealing/ -> models/ -> job/ -> the repo root.
    root = pathlib.Path(annealing_model.__file__).resolve().parents[3]
    with (root / "pyproject.toml").open("rb") as fh:
        pyproject = tomllib.load(fh)

    extra = pyproject["tool"]["dbx-leaning"]["models"]["annealing"]
    assert pyproject["project"]["optional-dependencies"][extra] == []


def test_it_never_touches_the_module_level_random():
    """The global RNG is shared process-wide. A model seeding or drawing from
    it would make its own answer depend on whatever else the job did — and
    would corrupt everyone else's stream in passing."""
    assert "random.seed" not in (PACKAGE / "model.py").read_text()

    random.seed(1)
    first = _solution(build_model(FAST))
    random.seed(999)
    for _ in range(50):
        random.random()
    second = _solution(build_model(FAST))
    assert first == second


# --- determinism -----------------------------------------------------------


def _solution(model) -> list[int]:
    model.emit = lambda type, **fields: None
    model.should_cancel = lambda: False
    model.run()
    return [row["item_index"] for row in model.results()]


def test_the_same_seed_gives_the_same_answer_every_time(recorder):
    runs = []
    for _ in range(3):
        model = recorder().attach(build_model(FAST))
        model.run()
        runs.append(model.results())
    assert runs[0] == runs[1] == runs[2]
    assert runs[0], "three identical empty results is not determinism"


def test_progress_is_deterministic_too(recorder):
    """Not only the answer: the whole telemetry stream has to replay, or a
    reported run cannot be reproduced from its record."""
    streams = []
    for _ in range(2):
        r = recorder()
        r.attach(build_model(FAST)).run()
        streams.append([(p["percent_complete"], p["primary_metric"]) for p in r.of("progress")])
    assert streams[0] == streams[1]


def test_a_different_seed_searches_differently(recorder):
    """If the seed did nothing, the 'seeded RNG' would be decoration."""
    a = _solution(build_model({**FAST, "seed": 1}))
    b = _solution(build_model({**FAST, "seed": 2}))
    assert a != b


def test_the_seed_is_on_the_result_rows(recorder):
    model = recorder().attach(build_model({**FAST, "seed": 4242}))
    model.run()
    assert all(row["seed"] == 4242 for row in model.results())


# --- it is a search, not a random number generator -------------------------


def test_it_beats_a_random_baseline(recorder):
    """Random-greedy fill packs the shift to capacity every time, so this is a
    real opponent, not a straw man. Losing to it would mean the annealing is
    an expensive way to shuffle."""
    model = recorder().attach(build_model())
    model.run()

    baseline = model.baseline_value
    assert baseline is not None and baseline > 0
    assert model.best_value > baseline, (
        f"annealing {model.best_value:.2f} did not beat random {baseline:.2f}"
    )
    improvement = 100.0 * (model.best_value - baseline) / baseline
    assert improvement > 5.0, f"only {improvement:.1f}% better than chance"


def test_the_improvement_is_recorded_on_the_results(recorder):
    model = recorder().attach(build_model(FAST))
    model.run()
    row = model.results()[0]
    assert row["baseline_objective"] == pytest.approx(model.baseline_value)
    assert row["improvement_over_baseline_pct"] > 0


def test_more_iterations_do_not_do_worse(recorder):
    """The incumbent only ever moves on an improvement, so a longer search
    cannot lose ground — a regression here means the best-tracking is wrong."""
    short = recorder().attach(build_model({**FAST, "iterations": 500}))
    short.run()
    long = recorder().attach(build_model({**FAST, "iterations": 20_000}))
    long.run()
    assert long.best_value >= short.best_value


def test_the_baseline_does_not_disturb_the_search(recorder):
    """Scoring the baseline draws thousands of random numbers. If it shared
    the search's RNG, asking for a comparison would change the answer."""
    with_baseline = _solution(build_model({**FAST, "baseline_trials": 500}))
    without = _solution(build_model({**FAST, "baseline_trials": 0}))
    assert with_baseline == without


# --- the solution is real --------------------------------------------------


def test_the_chosen_shift_fits_in_the_shift(recorder):
    model = recorder().attach(build_model(FAST))
    model.run()

    row = model.results()[0]
    assert row["total_weight"] <= model.problem.capacity + 1e-9
    assert row["items_selected"] == len(model.results())


def test_the_incremental_arithmetic_matches_the_definition(recorder):
    """The search updates value and weight move by move rather than
    recomputing. Floating-point drift over 30,000 updates would be invisible
    except here."""
    model = recorder().attach(build_model(FAST))
    model.run()

    value, weight, objective = model.evaluate(model.best_selection)
    assert value == pytest.approx(model.best_value)
    assert objective == pytest.approx(model.best_value)  # feasible: no penalty
    assert weight <= model.problem.capacity + 1e-9


def test_going_over_the_shift_is_penalised(fixed_data):
    """The penalty is what lets the search walk through infeasible states
    without ever preferring one."""
    model = build_model(FAST)
    capacity = model.problem.capacity
    best_density = max(
        v / w for v, w in zip(model.problem.values, model.problem.weights, strict=True)
    )

    at_capacity = model.objective(100.0, capacity)
    over = model.objective(100.0 + best_density, capacity + 1.0)
    assert model.objective(100.0, capacity - 5.0) == 100.0  # under: no charge
    assert over < at_capacity, "an overrun paid for itself"


def test_results_are_ranked_by_value_density(recorder):
    model = recorder().attach(build_model(FAST))
    model.run()
    rows = model.results()
    assert [row["rank"] for row in rows] == list(range(len(rows)))
    densities = [row["value_density"] for row in rows]
    assert densities == sorted(densities, reverse=True)


# --- telemetry: the non-monotonic part -------------------------------------


def test_progress_does_not_flood(recorder):
    """Thousands of iterations at microseconds each. One message per
    iteration would swamp the channel and say nothing a sampled curve does
    not."""
    r = recorder()
    r.attach(build_model({**FAST, "iterations": 20_000, "progress_every": 1_000})).run()

    progress = r.of("progress")
    assert 1 <= len(progress) <= 40, f"{len(progress)} progress messages for 20,000 iterations"


def test_percent_complete_climbs_to_100(recorder):
    r = recorder()
    r.attach(build_model(FAST)).run()
    percents = [p["percent_complete"] for p in r.of("progress")]
    assert percents == sorted(percents)
    assert percents[-1] == pytest.approx(100.0)
    assert all(0 < p <= 100 for p in percents)


def test_percent_complete_reaches_100_even_on_a_ragged_batch_size(recorder):
    """A curve that stops at 97% reads as a run that died partway."""
    r = recorder()
    r.attach(build_model({**FAST, "iterations": 4_321, "progress_every": 1_000})).run()
    assert r.of("progress")[-1]["percent_complete"] == pytest.approx(100.0)


def test_the_primary_metric_is_the_best_and_only_ever_improves(recorder):
    """A generic progress view knows nothing about annealing. Leading with the
    current objective would draw it a sawtooth and look like a broken run; the
    best-so-far is monotonic and reads correctly with no model-specific code."""
    r = recorder()
    r.attach(build_model(FAST)).run()

    progress = r.of("progress")
    assert all(p["primary_metric_label"] == "best_fare" for p in progress)
    best = [p["primary_metric"] for p in progress]
    assert best == sorted(best), "the best-so-far went backwards"


def test_the_current_objective_does_get_worse(recorder):
    """The thing that makes this model's telemetry different: uphill moves are
    accepted on purpose. If the current objective never fell, either the
    payload is reporting the incumbent by mistake or the acceptance rule has
    stopped accepting."""
    r = recorder()
    r.attach(build_model({**FAST, "progress_every": 25, "progress_every_s": 1e9})).run()

    current = [p["payload"]["current_objective"] for p in r.of("progress")]
    worsened = sum(1 for before, after in zip(current, current[1:], strict=False) if after < before)
    assert worsened > 0, "the search never accepted a worse state — that is not annealing"


def test_the_payload_carries_what_a_specific_view_needs(recorder):
    r = recorder()
    r.attach(build_model(FAST)).run()

    payloads = [p["payload"] for p in r.of("progress")]
    for key in ("temperature", "current_objective", "acceptance_rate", "iteration", "feasible"):
        assert all(key in p for p in payloads), key
    assert all(0.0 <= p["acceptance_rate"] <= 1.0 for p in payloads)


def test_the_temperature_cools(recorder):
    r = recorder()
    r.attach(build_model(FAST)).run()
    temperatures = [p["payload"]["temperature"] for p in r.of("progress")]
    assert temperatures == sorted(temperatures, reverse=True)
    assert temperatures[-1] < temperatures[0]


def test_cooling_makes_the_search_pickier(recorder):
    """Acceptance should fall as the temperature does. A flat acceptance rate
    would mean the temperature is not reaching the acceptance rule."""
    r = recorder()
    r.attach(build_model({"iterations": 20_000, "n_items": 120, "baseline_trials": 10})).run()

    rates = [p["payload"]["acceptance_rate"] for p in r.of("progress")]
    first, last = rates[: len(rates) // 4], rates[-len(rates) // 4 :]
    assert sum(first) / len(first) > sum(last) / len(last)


def test_every_message_is_a_legal_envelope_message(recorder):
    r = recorder()
    r.attach(build_model(FAST)).run()
    r.validate_all()


# --- cancellation ----------------------------------------------------------


def test_cancelling_mid_search_keeps_the_best_so_far(recorder):
    r = recorder(cancel_after=6)
    model = r.attach(build_model({"iterations": 200_000, "progress_every": 200}))
    model.run()

    rows = model.results()
    assert rows, "cancellation threw away the incumbent"
    assert model.cancelled is True
    assert 0 < model.iterations_run < 200_000
    assert all(row["cancelled"] is True for row in rows)
    assert all(row["iterations_run"] == model.iterations_run for row in rows)
    assert rows[0]["total_weight"] <= model.problem.capacity + 1e-9


def test_a_cancelled_solution_is_still_a_real_solution(recorder):
    r = recorder(cancel_after=6)
    model = r.attach(build_model({"iterations": 200_000, "progress_every": 200}))
    model.run()

    value, _weight, _objective = model.evaluate(model.best_selection)
    assert value == pytest.approx(model.best_value)
    assert model.results()[0]["baseline_objective"] is not None


def test_cancellation_before_the_first_iteration_is_clean(recorder):
    r = recorder()
    r.cancel()
    model = r.attach(build_model(FAST))
    model.run()

    assert model.iterations_run == 0
    assert model.results() == []  # nothing chosen, and no exception
    r.validate_all()


# --- provenance ------------------------------------------------------------


def test_provenance_is_logged_at_the_input_phase(fixed_data, recorder):
    r = recorder()
    r.attach(build_model(FAST)).run()

    inputs = [m for m in r.of("log") if m["phase"] == "input"]
    assert any(m["message"] == fixed_data.provenance for m in inputs), inputs
    assert any("trips on offer" in m["message"] for m in inputs)


def test_result_rows_say_which_data_they_came_from(fixed_data, recorder):
    model = recorder().attach(build_model(FAST))
    model.run()

    row = model.results()[0]
    assert row["data_source"] == "samples.nyctaxi.trips"
    assert row["data_synthetic"] is False
    assert row["data_rows"] == len(fixed_data.rows)
    assert row["data_fallback_reason"] is None
    assert row["items_offered"] == len(fixed_data.rows)


def test_a_synthetic_fallback_is_visible_in_the_results(monkeypatch, recorder):
    monkeypatch.setattr(
        annealing_model, "nyc_taxi_trips", lambda **kw: fake_dataset(synthetic=True)
    )
    model = recorder().attach(build_model(FAST))
    model.run()

    row = model.results()[0]
    assert row["data_synthetic"] is True
    assert row["data_fallback_reason"] == "no workspace"


def test_the_data_is_loaded_once(monkeypatch, recorder):
    calls = []

    def counting(**kwargs):
        calls.append(kwargs)
        return fake_dataset()

    monkeypatch.setattr(annealing_model, "nyc_taxi_trips", counting)
    model = recorder().attach(build_model(FAST))
    model.build()
    model.run()
    model.results()

    assert len(calls) == 1, f"loaded the trips {len(calls)} times"


def test_the_knapsack_comes_from_the_data(fixed_data):
    model = build_model(FAST)
    problem = model.problem

    assert list(problem.values) == [row["fare_amount"] for row in fixed_data.rows]
    assert list(problem.weights) == [row["duration_min"] for row in fixed_data.rows]
    assert problem.capacity == pytest.approx(
        0.25 * math.fsum(row["duration_min"] for row in fixed_data.rows)
    )


def test_an_empty_dataset_does_not_pretend_to_have_solved_anything(monkeypatch, recorder):
    monkeypatch.setattr(
        annealing_model,
        "nyc_taxi_trips",
        lambda **kw: Dataset(rows=[], source="synthetic:test", synthetic=True, reason="empty"),
    )
    r = recorder()
    model = r.attach(build_model(FAST))
    model.run()

    assert model.results() == []
    assert any(m["level"] == "WARNING" for m in r.of("log"))
