"""The CP-SAT job shop. Standalone — no transport, no Databricks.

What this suite defends that the Gurobi suites do not:

* **The solver is CP-SAT, not the legacy MPSolver wrapper.** `pywraplp` would
  be branch-and-bound on an LP relaxation wearing a different name, which is
  the one thing this model must not be. An AST scan says so, because an import
  swapped in a hurry is invisible in review.
* **The instance is derived, and the derivation is guarded.** Every bakehouse
  column is nullable and `quantity` is a LONG, so the tests state exactly which
  rows become jobs, which are skipped, and what a clamp does — including the
  case where *nothing* is usable.
* **Progress is sampled, not streamed.** A portfolio search can improve dozens
  of times in a second; a test asserts the message count stays far below the
  incumbent count, and that the last incumbent still reaches the record.
* **A cancelled run keeps its incumbent**, and INFEASIBLE is produced, returned
  as a real `RunStatus` member, and told apart from the detail strings that
  deliberately are not.
"""

from __future__ import annotations

import ast
import pathlib
import re
import subprocess
import sys

import pytest

pytest.importorskip("ortools", reason="needs the [ortools] extra")

from models._data import Dataset  # noqa: E402
from models.ortools_jobshop import (  # noqa: E402
    MAX_JOBS,
    RECIPES,
    STAGES,
    Instance,
    build_instance,
    build_model,
    operation_ceiling_for,
    recipe_for,
)
from models.ortools_jobshop import instance as instance_module  # noqa: E402
from models.ortools_jobshop.instance import (  # noqa: E402
    MAX_OPERATION_MINUTES,
    MAX_UNITS,
    MIN_OPERATION_MINUTES,
    OVEN_BATCH_UNITS,
)

PACKAGE = pathlib.Path(instance_module.__file__).parent
DDL = pathlib.Path(__file__).resolve().parents[2] / "uc_ddl" / "002_model_results.sql"

#: Small, deterministic, and quick enough to solve several times in a suite.
FAST = {"max_jobs": 12, "use_sample_data": False, "workers": 1, "max_time_in_seconds": 10}


# --- helpers ----------------------------------------------------------------


def sales_rows(n=20, *, units=12, product="Tokyo Tidbits", overrides=None):
    """`bakery_batches`-shaped rows, so a test can state exactly what the
    derivation is supposed to be looking at."""
    rows = [
        {
            "production_day": "2026-08-01",
            "franchise_id": 3000 + index,
            "product": product,
            "units": units,
            "orders": 3,
        }
        for index in range(n)
    ]
    for index, patch in (overrides or {}).items():
        rows[index].update(patch)
    return rows


def dataset(rows, *, synthetic=False, source=None, reason=None):
    return Dataset(
        rows=rows,
        source=source or instance_module.SALES_TABLE,
        synthetic=synthetic,
        reason=reason,
    )


def solved(config=None, recorder_cls=None, recorder=None):
    """Build, wire the two callables, run. The entire harness."""
    if recorder is None:
        recorder = (recorder_cls or _recorder_class())()
    model = recorder.attach(build_model({**FAST, **(config or {})}))
    outcome = model.run()
    return recorder, model, outcome


def _recorder_class():
    from tests.models.conftest import Recorder

    return Recorder


@pytest.fixture(scope="module")
def default_run():
    """One solve shared by every read-only assertion about a good schedule."""
    return solved()


def ddl_columns(table: str) -> list[str]:
    """Column names of a CREATE TABLE in the results DDL.

    Read rather than restated: the DDL is the contract for the result rows, and
    a test that keeps its own copy of it cannot catch the two drifting apart.
    """
    text = DDL.read_text()
    start = text.index(f"CREATE TABLE IF NOT EXISTS main.dbx_leaning.{table}")
    body = text[text.index("(", start) + 1 : text.index("\n)", start)]
    names = []
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("--"):
            continue
        match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s+[A-Z]", line)
        if match:
            names.append(match.group(1))
    return names


# --- the solver is the one this model exists for ----------------------------


def _imported_modules() -> set[str]:
    names: set[str] = set()
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                names.add(node.module)
    return names


def test_the_solver_is_cp_sat_and_not_the_legacy_mpsolver_wrapper():
    """`ortools.linear_solver.pywraplp` is CBC/SCIP/GLOP behind an MPSolver
    facade — branch-and-bound on an LP relaxation, which is what the two Gurobi
    models already are. If this ever fails, the model has lost its reason to
    exist; fix the import rather than relaxing the assertion."""
    imported = _imported_modules()
    assert "ortools.sat.python" in {name.rsplit(".", 1)[0] for name in imported} or any(
        name.startswith("ortools.sat.python") for name in imported
    )
    assert not any("linear_solver" in name or "pywraplp" in name for name in imported)


def test_the_harness_sees_the_surface_it_needs():
    from job.loader import describe_object

    handle = describe_object(build_model(FAST), "models.ortools_jobshop")
    assert handle.run is not None and handle.results is not None
    assert handle.build is not None
    assert handle.results_table == "results_ortools_jobshop"
    # Not the Gurobi path: this model drives its own solve, so the loader must
    # not find a `grb_model` to take over.
    assert handle.gurobi_model is None


# --- the instance: what is derived, and what the guards do ------------------


def test_units_drive_the_durations_that_should_scale():
    small = build_instance(max_jobs=1, sales_data=dataset(sales_rows(units=4)))
    large = build_instance(max_jobs=1, sales_data=dataset(sales_rows(units=200)))
    by_stage = lambda inst: {op.stage: op.minutes for op in inst.jobs[0].operations}  # noqa: E731
    small_ops, large_ops = by_stage(small), by_stage(large)

    for stage in ("mix", "pack"):
        assert large_ops[stage] > small_ops[stage], stage
    if "decorate" in small_ops:
        assert large_ops["decorate"] > small_ops["decorate"]


def test_bake_steps_with_the_oven_rather_than_scaling_with_the_count():
    """The one stage that is deliberately not linear: a tray is a tray."""
    one_tray = build_instance(max_jobs=1, sales_data=dataset(sales_rows(units=OVEN_BATCH_UNITS)))
    two_trays = build_instance(
        max_jobs=1, sales_data=dataset(sales_rows(units=OVEN_BATCH_UNITS + 1))
    )
    bake = lambda inst: next(  # noqa: E731
        op.minutes for op in inst.jobs[0].operations if op.stage == "bake"
    )
    assert bake(two_trays) == 2 * bake(one_tray)


def test_a_null_quantity_never_reaches_the_arithmetic():
    """`float(None)` on a real NULL is a defect this repo has shipped once."""
    rows = sales_rows(6, overrides={0: {"units": None}, 1: {"product": None}})
    inst = build_instance(max_jobs=6, sales_data=dataset(rows))
    assert inst.job_count == 4
    assert inst.data_meta["data_rows"] == 4  # dropna's count, not the table's


@pytest.mark.parametrize("bad", [0, -5, "not a number", float("nan")])
def test_a_quantity_that_cannot_be_a_positive_int_never_becomes_a_job(bad):
    """Two layers, and which one catches a given value is not the point: NaN is
    dropped by `dropna` (so it never reaches the row loop), a zero survives it
    and is skipped by the coercion. Either way the row is *counted*, never
    silently turned into a zero-length operation."""
    rows = sales_rows(5, overrides={0: {"units": bad}})
    inst = build_instance(max_jobs=5, sales_data=dataset(rows))
    assert inst.job_count == 4
    dropped = 5 - inst.data_meta["data_rows"]
    assert dropped + inst.instance_meta["rows_skipped_unusable"] == 1


def test_an_absurd_quantity_is_clamped_and_the_clamp_is_reported():
    rows = sales_rows(3, overrides={0: {"units": 10_000_000}})
    inst = build_instance(max_jobs=3, sales_data=dataset(rows))
    assert inst.instance_meta["quantities_clamped"] == 1
    assert inst.jobs[0].units == MAX_UNITS
    for job in inst.jobs:
        for op in job.operations:
            assert MIN_OPERATION_MINUTES <= op.minutes <= MAX_OPERATION_MINUTES


def test_no_usable_rows_falls_back_rather_than_scheduling_nothing():
    """A table that answers with nothing usable is a fallback, not an empty
    shop floor — and the reason survives onto every result row."""
    inst = build_instance(max_jobs=4, sales_data=dataset([{"product": None, "units": None}]))
    assert inst.job_count == 4
    assert inst.data_meta["data_synthetic"] is True
    assert inst.data_meta["data_fallback_reason"]


def test_the_size_cap_keeps_the_busiest_and_says_what_it_dropped():
    rows = sales_rows(30)
    inst = build_instance(max_jobs=7, sales_data=dataset(rows))
    assert inst.job_count == 7
    assert inst.instance_meta["batches_offered"] == 30
    assert inst.instance_meta["batches_capped"] == 23
    # Counted over the jobs actually built, not over the whole candidate list:
    # "7 jobs" and "7 jobs standing for 21 transactions" are different claims.
    assert inst.instance_meta["transactions_behind_jobs"] == 7 * 3


def test_the_size_cap_refuses_rather_than_clipping():
    with pytest.raises(ValueError) as excinfo:
        build_instance(max_jobs=MAX_JOBS + 1)
    message = str(excinfo.value)
    assert str(operation_ceiling_for(MAX_JOBS + 1)) in message
    # The cap is about the job task's hour. Saying so matters: the whole point
    # of this model is that the *solver* has no cap.
    assert "no variable or constraint cap" in message


def test_recipes_visit_the_machines_in_different_orders():
    """Otherwise this is a flow shop and `add_no_overlap` is doing far less
    work than the module claims."""
    orders = {tuple(STAGES.index(stage) for stage in stages) for stages in RECIPES.values()}
    assert len(orders) == len(RECIPES)
    # `rest` before the oven in one recipe and after it in another is the
    # specific thing that stops the machine order being shared.
    positions = {
        name: (stages.index("rest") - stages.index("bake"))
        for name, stages in RECIPES.items()
        if "rest" in stages
    }
    assert min(positions.values()) < 0 < max(positions.values())


def test_a_products_recipe_does_not_change_between_processes():
    """`hash()` on a str is randomised per process. If `recipe_for` ever used
    it, the same product would get a different recipe on every run and the
    instance would stop being reproducible for no reason at all."""
    code = (
        "from models.ortools_jobshop import recipe_for; "
        "print(','.join(recipe_for(p) for p in "
        "('Golden Gate Ginger', 'Pearly Pies', 'Reykjavik Rye')))"
    )
    seen = {
        subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
            env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
            cwd=str(PACKAGE.parents[1]),
        ).stdout.strip()
        for seed in ("0", "1", "12345")
    }
    assert len(seen) == 1, f"recipe assignment is process-dependent: {seen}"
    assert seen.pop().split(",")[0] == recipe_for("Golden Gate Ginger")


def test_the_lower_bound_is_the_larger_of_the_two_trivial_ones():
    inst = build_instance(max_jobs=9, sales_data=dataset(sales_rows(20)))
    longest_job = max(job.total_minutes for job in inst.jobs)
    busiest_machine = max(inst.machine_load(m) for m in range(inst.machine_count))
    assert inst.makespan_lower_bound == max(longest_job, busiest_machine)


def test_the_query_reads_only_verified_columns_and_no_money():
    """`bakehouse` is the one schema whose columns are verified
    (docs/sample-data-inventory.md), and `unitPrice`/`totalPrice` are LONGs
    whose units are not. Nothing here depends on them, deliberately."""
    captured = {}

    def fake_load(sql, **kwargs):
        captured["sql"] = sql
        captured["source"] = kwargs["source"]
        return Dataset(rows=list(kwargs["fallback"]()), source="synthetic:test", synthetic=True)

    original = instance_module.load
    instance_module.load = fake_load
    try:
        data = instance_module.bakery_batches(limit=12)
    finally:
        instance_module.load = original

    sql = captured["sql"]
    assert captured["source"] == "samples.bakehouse.sales_transactions"
    for column in ("dateTime", "franchiseID", "product", "quantity"):
        assert column in sql
    for money in ("unitPrice", "totalPrice"):
        assert money not in sql
    # A LIMIT without a total order returns a different instance every run.
    assert "ORDER BY" in sql
    # The fallback has to be the same shape as the query, or the model behaves
    # differently offline than on a workspace — which is the whole point of it.
    assert set(data.rows[0]) == {"production_day", "franchise_id", "product", "units", "orders"}


# --- the schedule -----------------------------------------------------------


def test_it_runs_standalone_with_nothing_but_two_callables(default_run):
    recorder, model, outcome = default_run
    assert model.results(), "a solved instance produced no rows"
    assert model.makespan is not None
    assert isinstance(outcome, str)
    recorder.validate_all()


def test_no_machine_does_two_things_at_once(default_run):
    _, model, _ = default_run
    by_machine: dict[int, list[tuple[int, int]]] = {}
    for row in model.results():
        by_machine.setdefault(row["machine_id"], []).append(
            (row["start_minute"], row["end_minute"])
        )
    for machine_id, spans in by_machine.items():
        spans.sort()
        for (_, end), (start, _) in zip(spans, spans[1:], strict=False):
            assert end <= start, f"machine {machine_id} overlaps"


def test_operations_within_a_job_stay_in_order(default_run):
    _, model, _ = default_run
    by_job: dict[int, list[dict]] = {}
    for row in model.results():
        by_job.setdefault(row["job_id"], []).append(row)
    for rows in by_job.values():
        rows.sort(key=lambda r: r["operation_index"])
        for previous, following in zip(rows, rows[1:], strict=False):
            assert previous["end_minute"] <= following["start_minute"]


def test_the_makespan_is_the_last_end_and_beats_no_bound_it_cannot(default_run):
    _, model, _ = default_run
    rows = model.results()
    assert model.makespan == max(row["end_minute"] for row in rows)
    assert model.makespan >= model.instance.makespan_lower_bound


def test_result_rows_carry_exactly_the_columns_the_ddl_declares(default_run):
    """Nothing else cross-checks these two: a model can pass the whole suite
    and then fail its first real write with a column mismatch."""
    _, model, _ = default_run
    # run_id and chunk_index are stamped by the harness, never by the model.
    expected = set(ddl_columns("results_ortools_jobshop")) - {"run_id", "chunk_index"}
    assert set(model.results()[0]) == expected


def test_provenance_travels_on_every_row(default_run):
    _, model, _ = default_run
    for row in model.results():
        assert row["data_source"]
        assert row["data_synthetic"] is True  # FAST reads nothing
        assert "data_fallback_reason" in row


# --- telemetry --------------------------------------------------------------


def test_progress_is_sampled_rather_than_one_message_per_incumbent():
    """A portfolio search improves far more often than a reader wants to be
    told. The rule is: the first solution, then a throttle, then one final
    sample whatever the throttle says."""
    recorder, model, _ = solved(
        {"max_jobs": 60, "progress_every_s": 3600.0, "max_time_in_seconds": 20}
    )
    progress = recorder.of("progress")
    assert model.solutions_found > len(progress), (
        f"{model.solutions_found} incumbents produced {len(progress)} messages"
    )
    # First solution, then nothing until the guaranteed final sample.
    assert len(progress) == 2
    assert progress[-1]["payload"]["final"] is True
    assert progress[-1]["payload"]["incumbent"] == float(model.makespan)


def test_the_last_incumbent_reaches_the_record_even_if_the_throttle_ate_it(default_run):
    recorder, model, _ = default_run
    final = recorder.of("progress")[-1]
    assert final["payload"]["incumbent"] == float(model.makespan)
    assert final["payload"]["solver_status"] == model.solver_status


def test_the_progress_payload_says_what_it_has_and_nothing_it_does_not(default_run):
    recorder, model, _ = default_run
    payload = recorder.of("progress")[-1]["payload"]
    for key in (
        "incumbent",
        "best_bound",
        "gap",
        "solutions_found",
        "wall_time",
        "n_jobs",
        "n_machines",
        "n_operations",
    ):
        assert key in payload
    assert payload["n_operations"] == model.instance.operation_count


def test_the_primary_metric_is_the_gap_and_is_not_called_a_mip_gap(default_run):
    """Same formula as `job/drivers/gurobi.py`'s `mip_gap`. A different name,
    because this is not a MIP and the contrast is the model's reason to be."""
    recorder, model, _ = default_run
    final = recorder.of("progress")[-1]
    assert final["primary_metric_label"] == "relative_gap"
    assert final["primary_metric"] == pytest.approx(0.0, abs=1e-9)  # proved optimal
    assert model.solver_status == "OPTIMAL"


def test_percent_complete_is_a_time_fraction_and_says_so(default_run):
    recorder, _, _ = default_run
    samples = recorder.of("progress")
    assert all(
        s["payload"]["percent_complete_basis"] == "elapsed_solver_time_against_time_limit"
        for s in samples
    )
    # A search that finished is 100% done however little of its budget it used.
    assert samples[-1]["percent_complete"] == 100.0
    assert 0.0 <= samples[0]["percent_complete"] <= 100.0


def test_percent_complete_is_null_while_running_with_no_time_limit():
    """No denominator, no honest fraction — null, which the frontend renders as
    indeterminate. A guess would be worse than nothing.

    The final sample is the exception, and not a contradiction: a search that
    terminated is 100% done whether or not anyone set a budget for it.
    """
    recorder, _, _ = solved({"max_time_in_seconds": None})
    samples = [s["percent_complete"] for s in recorder.of("progress")]
    assert samples[:-1] == [None] * (len(samples) - 1)
    assert samples[-1] == 100.0


def test_the_solver_log_is_captured_durably_and_not_streamed(default_run):
    recorder, model, _ = default_run
    solver_lines = [line for line in recorder.of("log") if line["source"] == "cp-sat"]
    assert solver_lines, "CP-SAT's own log was not captured"
    assert model.solver_log_lines == len(solver_lines)
    # Dense worker-by-worker chatter: worth keeping, not worth streaming.
    assert all(line["client_visible"] is False for line in solver_lines)
    # Split into lines and right-trimmed: a half-line in the log table is
    # worse than no line at all.
    assert all(line["message"] == line["message"].rstrip() for line in solver_lines)
    assert not any("\n" in line["message"] for line in solver_lines)


def test_the_solver_log_can_be_turned_off():
    recorder, model, _ = solved({"solver_log": False})
    assert model.solver_log_lines == 0
    assert not [line for line in recorder.of("log") if line["source"] == "cp-sat"]
    assert model.results(), "turning the log off must not change the answer"


# --- cancellation -----------------------------------------------------------


def test_a_cancelled_run_keeps_its_incumbent():
    """Cancellation is a clean outcome, not an error: whatever schedule the
    search had reached is still written."""
    recorder_cls = _recorder_class()
    recorder, model, outcome = solved(
        # 60 jobs so the search is still going when the cancel lands.
        {"max_jobs": 60, "max_time_in_seconds": 30},
        recorder=recorder_cls(cancel_on="progress"),
    )
    assert model.cancelled is True
    assert model.results(), "a cancelled run lost its incumbent"
    assert model.makespan is not None
    assert model.wall_time < 30.0, "the cancel did not stop the search"
    recorder.validate_all()


def test_a_cancelled_run_does_not_claim_to_have_finished():
    recorder_cls = _recorder_class()
    recorder, model, _ = solved(
        {"max_jobs": 60, "max_time_in_seconds": 30},
        recorder=recorder_cls(cancel_on="progress"),
    )
    final = recorder.of("progress")[-1]
    # It stopped early: the honest number is the fraction of the budget it
    # actually used, not 100.
    assert final["percent_complete"] < 100.0


def test_cancellation_is_polled_even_when_no_solution_is_ever_found():
    """A solution callback only fires on an improving solution. Without the
    other two polls a cancel would wait out the whole time limit."""
    recorder_cls = _recorder_class()
    recorder = recorder_cls()
    recorder.cancel()  # cancelled before the solve even starts
    model = recorder.attach(build_model({**FAST, "max_jobs": 60, "max_time_in_seconds": 30}))
    model.run()
    assert model.cancelled is True
    assert model.wall_time < 10.0


# --- terminal statuses ------------------------------------------------------


def test_an_impossible_deadline_is_reported_as_infeasible():
    from shared.envelope import RunStatus

    reference = build_instance(max_jobs=8, use_sample_data=False)
    _, model, outcome = solved(
        {
            "max_jobs": 8,
            # Below the trivial lower bound, so no schedule can exist. This is
            # the only route to INFEASIBLE: an open-horizon job shop can always
            # just run its jobs one after another.
            "deadline_minutes": max(1, reference.makespan_lower_bound // 2),
        }
    )
    assert outcome == "INFEASIBLE"
    assert RunStatus(outcome) is RunStatus.INFEASIBLE
    assert model.results() == []


def test_the_only_status_returns_are_real_run_status_members():
    """The other half of the trap below: these two strings are meant to be read
    as statuses, so they have to stay spelled exactly like the enum."""
    from models.ortools_jobshop.model import STATUS_RETURNS
    from shared.envelope import RunStatus

    assert [RunStatus(name).value for name in STATUS_RETURNS] == list(STATUS_RETURNS)


def test_an_ordinary_outcome_is_returned_as_a_detail_not_a_status(default_run):
    """The trap in models/README.md, asserted rather than trusted: a string
    that is not a `RunStatus` member becomes a *detail* on a SUCCEEDED run. The
    outcome strings below rely on that; the status strings must not drift into
    it."""
    from shared.envelope import RunStatus

    _, _, outcome = default_run
    assert "optimal" in outcome
    with pytest.raises(ValueError):
        RunStatus(outcome)


def test_an_empty_shop_floor_is_reported_rather_than_solved():
    """Reachable: rows that survive `dropna` but cannot become a positive
    quantity are skipped, and a table of nothing but those leaves no jobs."""
    empty = build_instance(max_jobs=4, sales_data=dataset(sales_rows(4, units=0)))
    assert empty.job_count == 0

    recorder, model, outcome = solved({"instance": empty})
    assert outcome == "no jobs to schedule"
    assert model.results() == []
    assert any(
        line["level"] == "WARNING" and "no jobs" in line["message"] for line in recorder.of("log")
    )
    recorder.validate_all()


def test_an_instance_can_be_handed_in_whole():
    """The config path a test — or a caller comparing solvers on one instance —
    needs. If this stops working, no two models can be run on the same problem.
    """
    inst = build_instance(max_jobs=5, sales_data=dataset(sales_rows(9, units=30)))
    _, model, _ = solved({"instance": inst})
    assert isinstance(model.instance, Instance)
    assert model.instance is inst
    assert {row["job_id"] for row in model.results()} == set(range(5))
