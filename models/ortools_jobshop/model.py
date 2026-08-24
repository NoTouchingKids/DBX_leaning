"""Job-shop scheduling with OR-Tools **CP-SAT**.

The solver is `ortools.sat.python.cp_model` — CP-SAT, the constraint
programming solver — and deliberately **not** `ortools.linear_solver.pywraplp`,
which is the legacy MPSolver wrapper around CBC/SCIP/GLOP. That distinction is
this model's whole reason to exist: pywraplp would be branch-and-bound on an LP
relaxation, the same paradigm as the two Gurobi models with a slower engine
behind it. CP-SAT is a different paradigm — a portfolio of search strategies
sharing learned clauses — and it is the one with no size cap. "OR-Tools" on its
own is ambiguous enough to be worth this paragraph.

**Why it is in the lineup**, next to `models/gurobi_scheduling` and
`models/gurobi_routing`:

* *A different solver paradigm.* Telemetry of the same shape — incumbent,
  bound, gap — arriving through a solution callback rather than a MIP callback.
  See "Progress" below for how differently it actually behaves.
* *No size cap.* The bundled Gurobi licence stops at 2000 variables / 2000
  constraints and expires on a date. CP-SAT has no limit, no licence file, no
  expiry, and nothing to reach over the network — which matters on a platform
  whose egress is restricted to trusted domains. A problem that outgrows
  `gurobi_scheduling` has somewhere to go, and this is it.
* *A different problem.* Not shift assignment. Jobs made of ordered
  operations, each needing a specific machine, each machine doing one thing at
  a time, minimising makespan. `add_no_overlap` over interval variables is what
  constraint programming is actually for: the same disjunction written as a MILP
  needs a big-M and a binary per pair of operations per machine.

**The harness does not drive this one.** `job/drivers/gurobi.py` exists because
Gurobi allows exactly one callback slot and the harness has to compose its own
observers with the model's. CP-SAT has no such contention — the callback is an
object we own — so this model exposes a plain blocking `run()` and drives the
solve itself, which is the ordinary case (`job/drivers/self_driving.py`).

**Progress.** A MIP callback fires constantly; a CP-SAT solution callback fires
once per *improving* incumbent, which can be a handful of times in total or a
burst of dozens in the first second. So the sampling rule has two halves:

* the first solution always reports, then at most one report every
  `progress_every_s` of solver wall time. Same reasoning as
  `job/drivers/gurobi.py`, different clock: CP-SAT hands us its own wall time,
  so the throttle and `elapsed_seconds` cannot drift apart.
* one final sample after the solve returns, unconditionally — otherwise the
  throttle can swallow the last and best incumbent, which is precisely the one
  a reader wants.

**`percent_complete` is a time fraction, and says so.** It is elapsed solver
time against the time limit, with `payload["percent_complete_basis"]` naming
that in the record. It is *not* a search fraction: nothing in a CP-SAT run
knows how much search is left. The final sample reports 100 only when the
search actually terminated on its own (proved optimal, proved infeasible, hit
its limit) — a cancelled run reports the fraction it really reached, because it
did not finish. With no time limit configured there is no honest denominator
and the field is null, which the frontend renders as indeterminate.

**Cancellation** is `should_cancel()` polled inside the solution callback,
then `stop_search()`. A second poll rides on CP-SAT's best-bound callback, so a
run that is grinding without finding any solution still stops promptly instead
of waiting out its time limit.

**Workers.** `num_workers = 0` is CP-SAT's default and means *decide from the
available cores* — not "disabled", which is what the name suggests. It is left
at 0 here: on Databricks serverless the core count is not this model's to
choose. The config field exists for the two cases where pinning it is right —
reproducibility (a portfolio search finds incumbents in a different order at
different worker counts, so the whole progress stream varies run to run; pin to
1 to compare like with like), and contention (five job tasks account-wide, each
of which would otherwise take every core).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from .instance import Instance, build_instance

__all__ = ["JobShopModel", "build_model", "DEFAULT_TIME_LIMIT_S"]

#: A run that cannot prove optimality still returns its incumbent. Better a
#: good schedule in a minute than an hour of a five-wide job pool spent closing
#: the last few minutes of makespan.
DEFAULT_TIME_LIMIT_S = 60.0

#: Jobs in a default instance. Enough that the shop floor is a real schedule
#: (~260 operations, a couple of seconds to prove optimal) and small enough
#: that the default run is never the thing that eats a job task's hour. The
#: regime where the search genuinely runs against the clock starts around 120
#: jobs — reachable from config here, and unreachable in the Gurobi models at
#: any setting, because that model would not fit the licence at all.
DEFAULT_MAX_JOBS = 60

#: At most one progress message per this many seconds of solver wall time,
#: after the first solution. See the module docstring for why the rule has to
#: be a throttle plus a guaranteed final sample rather than just a throttle.
PROGRESS_EVERY_S = 2.0

#: The two strings `run()` may return that are real `RunStatus` members and are
#: therefore read as a *status*. Everything else it returns is a detail on a
#: SUCCEEDED run — deliberately, that is how a detail is supplied — so these
#: two are kept apart by name to stop a future edit blurring the line
#: (models/README.md, job/drivers/self_driving.py).
STATUS_RETURNS = ("INFEASIBLE", "FAILED")


class JobShopModel:
    #: Discovered by the harness (see models/README.md). The DDL for it is in
    #: uc_ddl/002_model_results.sql and is the contract for `results()`.
    results_table = "results_ortools_jobshop"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = dict(config or {})
        self.instance: Instance = cfg.pop("instance", None) or build_instance(
            max_jobs=int(cfg.get("max_jobs", DEFAULT_MAX_JOBS)),
            seed=int(cfg.get("seed", 20260824)),
            # Batches come from the sample catalog by default. Turn it off for
            # a run that must not read anything at all.
            use_sample_data=bool(cfg.get("use_sample_data", True)),
            deadline_minutes=_optional_int(cfg.get("deadline_minutes")),
        )
        self.max_time_in_seconds = _optional_float(
            cfg.get("max_time_in_seconds", DEFAULT_TIME_LIMIT_S)
        )
        #: 0 means "use the available cores" — see the module docstring.
        self.workers = int(cfg.get("workers", 0))
        self.progress_every_s = float(cfg.get("progress_every_s", PROGRESS_EVERY_S))

        #: Set by the harness before build/run. Never imported from the platform.
        self.emit: Callable[..., Any] | None = None
        self.should_cancel: Callable[[], bool] | None = None

        self._sat_model: Any = None
        #: (job_index, operation_index) -> start variable. The schedule is read
        #: back through these, so they are the model's real output.
        self._starts: dict[tuple[int, int], Any] = {}
        self._makespan_var: Any = None

        #: Observable so a run — and a test — can say what the search did.
        self.solutions_found = 0
        self.makespan: int | None = None
        self.best_bound: float | None = None
        self.wall_time: float = 0.0
        self.solver_status: str = "NOT_SOLVED"
        self.cancelled = False
        self._schedule: list[dict[str, Any]] = []
        self._last_progress_at = 0.0
        self._search_completed = False

    # --- build ------------------------------------------------------------

    def build(self) -> None:
        from ortools.sat.python import cp_model

        inst = self.instance
        # Where the batches came from, before anything else — a run on real
        # sales and a run that fell back must not look the same.
        self._log(inst.provenance, phase="input")
        meta = inst.instance_meta
        self._log(
            f"{inst.job_count} jobs / {inst.operation_count} operations over "
            f"{inst.machine_count} machines ({', '.join(inst.machines)}); "
            f"{inst.total_minutes} machine-minutes of work, lower bound on makespan "
            f"{inst.makespan_lower_bound} min",
            phase="input",
        )
        if meta.get("batches_capped"):
            # The cap is a wall-clock decision and it drops real work. Say so.
            self._log(
                f"{meta['batches_offered']} candidate batches offered; kept the "
                f"{meta['jobs_built']} busiest and capped {meta['batches_capped']} "
                f"(max_jobs). CP-SAT has no size limit — this cap is the job task's hour.",
                phase="input",
                level="WARNING",
            )
        if meta.get("rows_skipped_unusable") or meta.get("quantities_clamped"):
            self._log(
                f"input guards: {meta.get('rows_skipped_unusable', 0)} rows skipped as "
                f"unusable, {meta.get('quantities_clamped', 0)} quantities clamped to "
                f"{meta.get('units_clamp')}",
                phase="input",
                level="WARNING",
            )

        model = cp_model.CpModel()
        self._starts = {}
        self._makespan_var = None
        self._sat_model = model

        if not inst.jobs:
            # An empty shop floor is not an error and not a schedule. Build
            # nothing, and let run() report it rather than asking CP-SAT for
            # the maximum of an empty set.
            self._log("no jobs to schedule", phase="build", level="WARNING")
            return

        horizon = inst.horizon
        ends: list[Any] = []
        per_machine: dict[int, list[Any]] = {}

        for job_index, job in enumerate(inst.jobs):
            previous_end = None
            for op_index, operation in enumerate(job.operations):
                start = model.new_int_var(0, horizon, f"s[{job_index},{op_index}]")
                end = model.new_int_var(0, horizon, f"e[{job_index},{op_index}]")
                # An interval variable is the whole reason to be in CP-SAT: it
                # carries start/size/end as one object that the scheduling
                # propagators reason about directly.
                interval = model.new_interval_var(
                    start, operation.minutes, end, f"i[{job_index},{op_index}]"
                )
                per_machine.setdefault(operation.machine_id, []).append(interval)
                if previous_end is not None:
                    # Precedence: operations within a job are ordered. Dough
                    # cannot be baked before it is mixed.
                    model.add(start >= previous_end)
                previous_end = end
                self._starts[(job_index, op_index)] = start
            ends.append(previous_end)

        # One machine, one thing at a time. As a MILP this is a big-M
        # disjunction and a binary per pair of operations per machine; here it
        # is one constraint per machine, and the solver's scheduling
        # propagators handle it natively.
        for intervals in per_machine.values():
            model.add_no_overlap(intervals)

        makespan = model.new_int_var(0, horizon, "makespan")
        model.add_max_equality(makespan, ends)
        if inst.deadline_minutes is not None:
            # The one constraint that can make this instance INFEASIBLE. A job
            # shop with an open horizon always has a schedule — run everything
            # end to end — so without a deadline that status is unreachable.
            model.add(makespan <= inst.deadline_minutes)
        model.minimize(makespan)
        self._makespan_var = makespan

        self._log(
            f"built: {inst.operation_count} interval variables, "
            f"{len(per_machine)} no-overlap constraints, horizon {horizon} min"
            + (f", deadline {inst.deadline_minutes} min" if inst.deadline_minutes else "")
            + ". CP-SAT has no variable or constraint cap; the Gurobi models stop at 2000/2000.",
            phase="build",
        )

    # --- solve ------------------------------------------------------------

    def run(self) -> str | None:
        """Solve, streaming progress from the solution callback.

        Returns a status string only where the harness could not infer one:
        ``"INFEASIBLE"`` and ``"FAILED"`` are real ``RunStatus`` members and
        are read as statuses. Anything else returned here is read as a *detail*
        on a SUCCEEDED run, which is how the run's outcome ("optimal",
        "time limit reached") reaches the record.
        """
        from ortools.sat.python import cp_model

        if self._sat_model is None:
            self.build()

        inst = self.instance
        self.solutions_found = 0
        self._schedule = []
        self._last_progress_at = 0.0
        self._search_completed = False
        self.cancelled = False

        if not inst.jobs:
            self.solver_status = "NO_JOBS"
            return "no jobs to schedule"

        solver = cp_model.CpSolver()
        parameters = solver.parameters
        if self.max_time_in_seconds:
            parameters.max_time_in_seconds = float(self.max_time_in_seconds)
        if self.workers:
            # 0 (the default) means "decide from the available cores", so it is
            # left unset rather than written back — see the module docstring.
            parameters.num_workers = self.workers
        # Reproducibility within a worker count. Across worker counts a
        # portfolio search is non-deterministic whatever the seed says.
        parameters.random_seed = inst.seed % (2**31 - 1)

        # The bound callback is the second cancellation poll: a solution
        # callback only fires on an *improving* solution, so a search that
        # finds nothing would otherwise ignore a cancel until its time limit.
        solver.best_bound_callback = lambda _bound: self._poll_cancel(solver.stop_search)

        recorder = _solution_recorder(cp_model, self._on_solution)
        self._log(
            f"solving with CP-SAT: limit "
            f"{self.max_time_in_seconds if self.max_time_in_seconds else 'none'}s, "
            f"workers {self.workers or 'auto'}, seed {inst.seed}",
            phase="solve",
        )

        status = solver.solve(self._sat_model, recorder)

        self.solver_status = solver.status_name(status)
        self.wall_time = float(solver.wall_time)
        self._search_completed = not self.cancelled
        self._read_solution(solver, status, cp_model)
        self._emit_progress(
            elapsed=self.wall_time,
            makespan=float(self.makespan) if self.makespan is not None else None,
            bound=self.best_bound,
            final=True,
        )
        self._log(
            f"{self.solver_status} after {self.wall_time:.2f}s and "
            f"{self.solutions_found} incumbent"
            f"{'' if self.solutions_found == 1 else 's'}"
            + (
                f": makespan {self.makespan} min, bound {self.best_bound:.0f}"
                if self.makespan is not None and self.best_bound is not None
                else ": no schedule"
            ),
            phase="solve",
        )
        return self._status_return(status, cp_model)

    def _on_solution(self, callback: Any) -> None:
        """One improving incumbent. Sampled, not reported wholesale."""
        self.solutions_found += 1
        elapsed = float(callback.wall_time)
        makespan = float(callback.objective_value)
        bound = _finite(callback.best_objective_bound)

        cancelling = self._poll_cancel(callback.stop_search)

        # First solution always: it is the one that turns an indeterminate
        # progress bar into a number. After that, throttled — a portfolio
        # search can improve dozens of times in the first second.
        due = elapsed - self._last_progress_at >= self.progress_every_s
        if self.solutions_found == 1 or due or cancelling:
            self._last_progress_at = elapsed
            self._emit_progress(
                elapsed=elapsed,
                makespan=makespan,
                bound=bound,
                conflicts=int(callback.num_conflicts),
                branches=int(callback.num_branches),
            )

    def _poll_cancel(self, stop: Callable[[], None]) -> bool:
        """True if a cancel was seen. Idempotent: `stop_search` is cheap but
        the log line is not, and both callbacks can fire after the first stop.
        """
        if self.cancelled:
            return True
        if self.should_cancel is None or not self.should_cancel():
            return False
        self.cancelled = True
        stop()
        self._log(
            "cancelled; stopping the search and keeping the best schedule found so far",
            phase="solve",
        )
        return True

    def _read_solution(self, solver: Any, status: Any, cp_model: Any) -> None:
        """Capture the incumbent once, after the solve.

        Reading it here rather than snapshotting inside every solution callback
        is deliberate: a stopped search still exposes its last solution (a
        `stop_search()` from the callback returns FEASIBLE), so the per-solution
        copy would cost O(operations) on every improvement and buy nothing.
        """
        self.makespan = None
        self.best_bound = None
        self._schedule = []
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            # No incumbent — INFEASIBLE, or stopped before the first solution.
            # Neither is an error and neither has a schedule to report.
            self.best_bound = _finite(solver.best_objective_bound)
            return

        self.makespan = int(round(solver.objective_value))
        self.best_bound = _finite(solver.best_objective_bound)
        for job_index, job in enumerate(self.instance.jobs):
            for op_index, operation in enumerate(job.operations):
                start = int(solver.value(self._starts[(job_index, op_index)]))
                self._schedule.append(
                    {
                        "job_id": job_index,
                        "job_label": job.label,
                        "operation_index": op_index,
                        "machine_id": operation.machine_id,
                        "machine_label": operation.stage,
                        "start_minute": start,
                        "duration_minutes": operation.minutes,
                        "end_minute": start + operation.minutes,
                    }
                )

    # --- telemetry --------------------------------------------------------

    def _emit_progress(
        self,
        *,
        elapsed: float,
        makespan: float | None,
        bound: float | None,
        conflicts: int | None = None,
        branches: int | None = None,
        final: bool = False,
    ) -> None:
        if self.emit is None:
            return
        gap = _relative_gap(makespan, bound)
        payload: dict[str, Any] = {
            "incumbent": makespan,
            "best_bound": bound,
            "gap": gap,
            "solutions_found": self.solutions_found,
            "wall_time": round(elapsed, 4),
            "n_jobs": self.instance.job_count,
            "n_machines": self.instance.machine_count,
            "n_operations": self.instance.operation_count,
            # Named in the record, not just in a docstring: this is a *time*
            # fraction, not a search fraction, and a reader six months from now
            # has only the row to go on.
            "percent_complete_basis": "elapsed_solver_time_against_time_limit",
            "final": final,
        }
        # Only what the solver actually hands over — no invented counters.
        if conflicts is not None:
            payload["conflicts"] = conflicts
        if branches is not None:
            payload["branches"] = branches
        if final:
            payload["solver_status"] = self.solver_status

        self.emit(
            "progress",
            elapsed_seconds=max(0.0, elapsed),
            percent_complete=self._percent_complete(elapsed, final=final),
            primary_metric=gap,
            # Not "mip_gap": the formula is Gurobi's, but this is not a MIP and
            # naming it one would misstate the single thing this model exists
            # to contrast. Same quantity, honest label.
            primary_metric_label="relative_gap",
            payload=payload,
        )

    def _percent_complete(self, elapsed: float, *, final: bool) -> float | None:
        """Elapsed against the time limit — or 100 once the search is done.

        A solve that proves optimality in 3 seconds of a 60-second budget is
        *finished*, and leaving its curve at 5% reads as a run that died. So
        the final sample reports 100 when the search terminated on its own, and
        the real fraction when it was cancelled — because then it genuinely did
        not finish.
        """
        if final and self._search_completed:
            return 100.0
        if not self.max_time_in_seconds:
            return None  # no denominator, so no honest fraction
        return max(0.0, min(100.0, 100.0 * elapsed / float(self.max_time_in_seconds)))

    def _status_return(self, status: Any, cp_model: Any) -> str | None:
        if status == cp_model.INFEASIBLE:
            # A real RunStatus member, so the harness reports it as a status.
            # Reachable here only through a deadline (see instance.py).
            return "INFEASIBLE"
        if status == cp_model.MODEL_INVALID:
            # The model this file built was rejected. That is a code defect,
            # not an outcome.
            return "FAILED"
        if status == cp_model.OPTIMAL:
            return f"optimal: makespan {self.makespan} min"
        if status == cp_model.FEASIBLE:
            gap = _relative_gap(
                float(self.makespan) if self.makespan is not None else None, self.best_bound
            )
            return (
                f"feasible: makespan {self.makespan} min"
                + (f", gap {gap * 100:.1f}%" if gap is not None else "")
            )
        return f"no schedule found ({self.solver_status})"

    # --- results ----------------------------------------------------------

    def results(self) -> list[dict[str, Any]]:
        """One row per scheduled operation — the grain the DDL declares.

        Never gated on OPTIMAL. A cancelled run and a run that hit its time
        limit both keep their incumbent, which is the platform's contract and
        the reason the schedule is read back from the solver's last solution
        rather than from a proof of optimality.
        """
        if not self._schedule:
            return []

        inst = self.instance
        # Provenance travels with every row: the results table is what someone
        # reads six months later, and "were these real sales?" is not a
        # question they should have to answer from the log stream.
        run_level = {
            "makespan": self.makespan,
            "best_bound": self.best_bound,
            "solver_status": self.solver_status,
            "solutions_found": self.solutions_found,
            "wall_time_seconds": round(self.wall_time, 4),
            "n_jobs": inst.job_count,
            "n_machines": inst.machine_count,
            "n_operations": inst.operation_count,
            "seed": inst.seed,
            "data_source": inst.data_meta.get("data_source"),
            "data_synthetic": inst.data_meta.get("data_synthetic"),
            "data_rows": inst.data_meta.get("data_rows"),
            "data_fallback_reason": inst.data_meta.get("data_fallback_reason"),
        }
        return [{**operation, **run_level} for operation in self._schedule]

    # --- helpers ----------------------------------------------------------

    def _log(self, message: str, *, phase: str = "input", level: str = "INFO") -> None:
        if self.emit is not None:
            self.emit("log", message=message, level=level, source="model", phase=phase)


def _solution_recorder(cp_model: Any, on_solution: Callable[[Any], None]) -> Any:
    """A `CpSolverSolutionCallback` that forwards to a plain method.

    Defined inside a function because the base class only exists once ortools
    is imported, and ortools is imported lazily so that the package can be
    introspected — `job.loader.describe_object`, a test collecting the module —
    in an environment that does not have it.
    """

    class _Recorder(cp_model.CpSolverSolutionCallback):  # type: ignore[misc]
        def on_solution_callback(self) -> None:
            on_solution(self)

    return _Recorder()


def _relative_gap(incumbent: float | None, bound: float | None) -> float | None:
    """``|incumbent - bound| / |incumbent|`` — the same formula the Gurobi
    driver calls ``mip_gap``. None when either side is unknown, and None rather
    than a division by zero at a makespan of nothing."""
    if incumbent is None or bound is None or abs(incumbent) < 1e-10:
        return None
    return abs(incumbent - bound) / abs(incumbent)


def _finite(value: Any) -> float | None:
    """A solver number, or None if it is not one. CP-SAT reports an infinite
    bound before it has proved anything, and an infinity on a chart axis is the
    same problem Gurobi's 1e100 sentinel causes."""
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def build_model(config: dict[str, Any] | None = None) -> JobShopModel:
    return JobShopModel(config)
