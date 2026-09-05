"""Simulated annealing over a knapsack — the zero-dependency control case.

**Why this model exists.** Every other model on `dev` pulls in a library:
gurobipy, scikit-learn, emcee, torch, numpy. This one pulls in nothing. Its
`dependencies` list is empty, exactly as the heartbeat's is, so if a deploy of
it breaks that is the platform's fault and not a library's — which is the whole
point of porting it first. `random`, `math` and `statistics` from the standard
library are the entire toolbox.

**Why annealing, telemetry-wise.** Every other model's progress curve only
improves: a MIP gap closes, a loss falls, R-hat settles. Annealing accepts
uphill moves *on purpose* — the current objective genuinely gets worse, often,
early on, and that is the algorithm working rather than failing. So the
progress stream needs two numbers, not one:

- ``primary_metric`` is the **best** objective so far. Monotonic, so a generic
  progress view with no annealing-specific code stays readable.
- ``payload`` carries the *current* objective, the temperature and the
  acceptance rate — the non-monotonic detail. A model-specific view plots
  current against best and shows the search cooling; the generic view ignores
  it. That split is exactly what `payload` is for.

**The problem.** A driver's shift: which trips to accept, given a fixed number
of minutes, to maximise fares. Items are real trips from Databricks' `samples`
catalog — value is the fare, weight is the trip's duration — or the loader's
deterministic fallback when there is no workspace (see `data.py`).

**Determinism.** A seeded ``random.Random``, never the module-level ``random``
— that global is shared process-wide, so anything else in the job touching it
would change this run's answer. Same seed, same trips, same solution, every
time. A stochastic search that cannot be reproduced cannot be debugged.

## What the template took over, and what a sweep is

The v3 version was 544 lines, and roughly a third of them were a run loop:
`attach`, cancel polling, batched progress emission, percentage arithmetic, a
start log and a finish log. `modelkit.Model` does all of that once, so what is
left here is the search, the instance, and the results.

The one structural consequence is worth stating plainly: **one `step` is one
sweep of `progress_every` Metropolis iterations, not one iteration.** An
iteration is microseconds, and the template emits one progress message per
step — a message per iteration would flood the channel and tell a reader
nothing that a sampled curve does not. Batching was v3's behaviour too; here it
falls out of what a step IS, rather than out of a counter and a wall clock
inside the loop.

That also makes the sweep the cancel granularity, which is why its size is
bounded from both ends in `prestep` rather than being taken on trust.

**The honest accounting, because "the template makes models shorter" is only
half true.** The loop really did go: v3's `run`, `__init__`, `attach` and
`_progress` were about 170 lines of code and are now about 90. But this model
also took over what the harness used to do for it — writing its own table,
naming its own columns, keying its own rows, building its own preview — and
that is about 100 lines that did not exist here before. Measured as code
rather than prose, v3 was 324 lines and this is 380. The template pays for
itself in every model; **owning your own results is charged once per model**,
and this is what that charge looks like.

## What this model does NOT declare, and why

v3 set ``results_table = "results_annealing"`` and returned rows from
``results()`` for the harness to write, and ``preview_axes`` for the harness to
downsample by. All three are gone. The harness writes no tables in v4 — the
model writes its own, in `poststep`, through `data.write_rows` — and it builds
its own bounded preview. `job/loader.py` still *discovers* those names, for
models that predate the change; declaring them here would advertise that the
harness is going to do something it will not.
"""

from __future__ import annotations

import math
import os
import random
import statistics
from dataclasses import dataclass
from typing import Any

from modelkit import CANCELLED, SUCCEEDED, Model

from .data import nyc_taxi_trips, write_rows

__all__ = ["Annealing", "Problem", "build_model", "RESULT_SCHEMA"]

#: How many trips are on offer. Small enough that a pure-Python sweep of
#: thousands of iterations finishes in well under a second, large enough that
#: the search space (2**240) is not enumerable and the annealing is doing real
#: work.
DEFAULT_ITEMS = 240
#: Shift length as a share of the total duration on offer. A knapsack is only
#: interesting when it binds: at 1.0 you take everything, at 0.01 you take the
#: single best trip. A quarter leaves a genuinely combinatorial choice.
DEFAULT_CAPACITY_FRACTION = 0.25
DEFAULT_ITERATIONS = 30_000
#: Iterations per sweep, i.e. per progress message and per cancel check. The
#: two bounds below can override it; see `prestep`.
DEFAULT_PROGRESS_EVERY = 1_000
#: Starting temperature as a multiple of the mean fare: a move that costs about
#: one average fare is accepted roughly 37% of the time at the start. Derived
#: from the data, not a magic constant, so it still makes sense if the loader
#: hands over a different fare distribution.
START_TEMPERATURE_FACTOR = 1.0
#: Geometric cooling ends here, as a fraction of the start. By the last
#: iterations the search is effectively hill-climbing.
END_TEMPERATURE_RATIO = 1e-3
#: Overweight is priced above the best value density on offer, so shedding an
#: overweight trip always pays and an infeasible state is never optimal. A
#: penalty rather than a hard constraint on purpose: letting the search cross
#: the capacity boundary is how it escapes a locally-full knapsack.
PENALTY_FACTOR = 2.0
#: Share of moves that swap one selected trip for one unselected trip rather
#: than flipping a single trip. Near a binding capacity almost every single
#: flip is rejected, so pure flips stall; a swap keeps the weight roughly
#: constant and keeps the search moving.
DEFAULT_SWAP_PROBABILITY = 0.5

#: Upper bound on progress messages per run, whatever `iterations` is set to.
#: A progress stream is a sampled curve, not a log of every iteration — the
#: envelope spec caps a *preview* at 1000 points for the same reason. Without
#: this, raising `iterations` a hundredfold silently raises the message rate a
#: hundredfold too, and the first thing to notice would be the live channel.
MAX_PROGRESS_POINTS = 500
#: Upper bound on one sweep, and it beats the bound above when they disagree.
#: The template polls for a cancel BETWEEN steps, so the sweep length IS the
#: cancel latency. An iteration measured 3.6us on the machine this was written
#: on, so this is about 0.4s — the same order as the ~100ms slice
#: `Model.sleep` uses, and the reason a configuration that made a sweep minutes
#: long is not allowed to: a cancel that lands minutes later reads as ignored.
MAX_SWEEP_ITERATIONS = 100_000

#: Bounded, because the envelope's preview is bounded (PREVIEW_MAX_POINTS is
#: 1000). Well under it: this is a chart, not the result set, and the full rows
#: are one query away through `fetch_hint`.
PREVIEW_POINTS = 200

#: The results table, column for column with `uc_ddl/002_model_results.sql`.
#: **Diff the two by hand when either changes** — nothing checks them against
#: each other, and the asymmetry matters: a column no row fills is harmless
#: clutter, while a row key with no column is a silently dropped field.
#:
#: `run_id` and `chunk_index` lead because they did in v3, where the HARNESS
#: stamped them. It does not any more, so this model fills them itself; that is
#: the single most easily-missed consequence of the model owning its own write.
RESULT_SCHEMA: tuple[tuple[str, str], ...] = (
    ("run_id", "STRING"),
    ("chunk_index", "INT"),
    # The chosen trips, ranked by value density (fare per minute) — the order
    # the preview curve is built in, not the search order.
    ("rank", "INT"),
    ("item_index", "INT"),
    ("value", "DOUBLE"),
    ("weight", "DOUBLE"),
    ("distance", "DOUBLE"),
    ("value_density", "DOUBLE"),
    # The solution this row belongs to, repeated per row: the results tables
    # are read flat, and a shift total is what a reader wants next to a trip.
    ("objective", "DOUBLE"),
    ("total_value", "DOUBLE"),
    ("total_weight", "DOUBLE"),
    ("items_selected", "INT"),
    # Planned vs run: a cancelled search stops early and still writes its
    # incumbent, so these two disagreeing is the record of that.
    ("iterations_run", "INT"),
    ("iterations_planned", "INT"),
    ("cancelled", "BOOLEAN"),
    # The seed is part of the result, not a footnote: without it a stochastic
    # search is not reproducible and the row cannot be checked.
    ("seed", "BIGINT"),
    # What random-greedy shift-filling achieved on the same instance. The
    # column that answers "was the search worth its iterations?" without
    # re-running anything.
    ("baseline_objective", "DOUBLE"),
    ("improvement_over_baseline_pct", "DOUBLE"),
    # The instance the search ran on.
    ("items_offered", "INT"),
    ("capacity_minutes", "DOUBLE"),
    ("total_weight_offered", "DOUBLE"),
    ("total_value_offered", "DOUBLE"),
    # Provenance of the trips. A run over real `samples` rows and one that fell
    # back to the deterministic generator must not look identical after the
    # fact — see `data.Dataset.describe`.
    ("data_source", "STRING"),
    ("data_synthetic", "BOOLEAN"),
    ("data_rows", "BIGINT"),
    ("data_fallback_reason", "STRING"),
)


@dataclass(frozen=True)
class Problem:
    """The knapsack instance, and where its numbers came from."""

    #: Fare per trip.
    values: tuple[float, ...]
    #: Duration in minutes per trip.
    weights: tuple[float, ...]
    #: Trip distance, carried through to the results for readability only.
    distances: tuple[float, ...]
    #: Minutes in the shift.
    capacity: float
    #: Charged per minute over capacity.
    penalty_rate: float
    #: One line for a log message, straight from the loader.
    provenance: str
    #: data_source / data_synthetic / data_rows / data_fallback_reason.
    data_fields: dict[str, Any]

    def __len__(self) -> int:
        return len(self.values)

    def describe(self) -> dict[str, Any]:
        return {
            "items_offered": len(self.values),
            "capacity_minutes": round(self.capacity, 6),
            "total_weight_offered": round(math.fsum(self.weights), 6),
            "total_value_offered": round(math.fsum(self.values), 6),
        }


class Annealing(Model):
    #: One step is one sweep of `progress_every` proposed moves. Reads
    #: correctly in "30 sweeps" and in a chart legend, and it is honest about
    #: the loop: "iterations" here would name something the template is not
    #: counting.
    unit = "sweeps"

    #: No wait between sweeps, and this is meaningful rather than a leftover
    #: default: for a heartbeat the interval IS the model, and for a search the
    #: work is the wait. A non-zero value here would make the run longer
    #: without making the answer better.
    interval = 0.0

    def configs(self) -> dict[str, Any]:
        return {
            "iterations": DEFAULT_ITERATIONS,
            "progress_every": DEFAULT_PROGRESS_EVERY,
            "seed": 20_260_823,
            "swap_probability": DEFAULT_SWAP_PROBABILITY,
            "capacity_fraction": DEFAULT_CAPACITY_FRACTION,
            # How many random-greedy shifts to score for comparison. This is
            # the number that answers "is this search worth its iterations, or
            # an expensive random number generator?" — so it is telemetry
            # rather than a test fixture, and it rides on the result rows.
            "baseline_trials": 200,
            # How much of the trip table to offer.
            "n_items": DEFAULT_ITEMS,
            "data_seed": 11,
            # Absolute overrides; None means "derive it from the data".
            "start_temperature": None,
            "end_temperature": None,
            # --- where the results go -------------------------------------
            #
            # Empty means "take it from the environment the job was given".
            # `run_model.py` puts every job parameter into `os.environ`, so
            # DBX_CATALOG and DBX_SCHEMA are there — the same bundle variables
            # that created the tables. The literal defaults below repeat
            # `JobConfig`'s, which is not duplication to be tidied away: a
            # model must not import the harness, so the two agree by having
            # been decided once and written twice.
            "catalog": "",
            "schema": "",
            "table": "results_annealing",
            # What the rows are keyed by. See `_resolve_run_id`.
            "run_id": "",
        }

    # --- setup -------------------------------------------------------------

    def prestep(self) -> None:
        """Read the trips, size the search, and say where results will go.

        All of it inside the run rather than in `__init__`, which is what
        `prestep` is for: a model that fails while loading its data has failed
        a run, and the log line that says which table it read is part of that
        run's record.
        """
        self.run_id = self._resolve_run_id()
        catalog = str(self.catalog or os.environ.get("DBX_CATALOG") or "main")
        schema = str(self.schema or os.environ.get("DBX_SCHEMA") or "dbx_leaning")
        self.target_table = f"{catalog}.{schema}.{self.table}"

        self._problem = self._load_problem()
        n = len(self._problem)

        self.iterations = max(0, int(self.iterations))
        # Two bounds, and the cancel bound wins where they disagree. Neither is
        # a tidy-up of the configured value: the first keeps the message rate
        # sane when `iterations` is raised for a long deployed run, the second
        # keeps a cancel from looking ignored when `progress_every` is raised.
        every = max(int(self.progress_every), 1)
        if self.iterations > MAX_PROGRESS_POINTS * every:
            every = math.ceil(self.iterations / MAX_PROGRESS_POINTS)
        self.progress_every = min(every, MAX_SWEEP_ITERATIONS)

        # `total` is sweeps, because a step is a sweep.
        #
        # Zero sweeps when there is nothing to search, and that has to be
        # decided HERE rather than inside `step`: the template runs `total`
        # steps whatever they contain, so an empty instance with a non-zero
        # total reaches `rng.randrange(0)` and fails a run over an empty table.
        # Zero instead sends it straight to `poststep`, which reports a
        # row_count of 0 — the honest answer to "the data had nothing in it".
        self.total = (
            math.ceil(self.iterations / self.progress_every) if self.iterations and n else 0
        )

        self._rng = random.Random(self.seed)  # never the module-level random
        self._selection = [False] * n
        self._selected: list[int] = []
        self._unselected = list(range(n))
        #: Position of each item in whichever of the two lists holds it, so a
        #: swap is O(1) rather than a list scan — the difference between an
        #: iteration costing microseconds and costing milliseconds.
        self._where = list(range(n))
        self._cur_value = 0.0
        self._cur_weight = 0.0
        self._cur_obj = 0.0
        self._accepted_total = 0
        self._iterations_run = 0

        #: The incumbent: the best *feasible* selection seen, and its value.
        self.best_selection: list[bool] = [False] * n
        self.best_value = 0.0
        self.baseline_value: float | None = None

        if n == 0:
            self.log("nothing to search: no usable trips", level="WARNING", phase="solve")
            return

        self.log(
            f"annealing {self.iterations} iterations over {n} trips in "
            f"{self.total} sweeps of {self.progress_every}, seed {self.seed}",
            phase="solve",
        )
        self.log(f"results will be written to {self.target_table} as {self.run_id}", phase="input")

    def _resolve_run_id(self) -> str:
        """What the result rows are keyed by.

        **The harness does not hand a model its run id.** `emit` stamps one on
        every message and the model never sees it, which is fine until the
        model writes a table of its own and has to key it the same way.

        So, in order: the config if a caller set one, then `DBX_RUN_ID` —
        which `run_model.py` copies out of the job parameters into
        `os.environ`, and which the app always sends. Reading an environment
        variable rather than importing anything is what keeps this model
        movable: that name belongs to the deployment, not to a package this
        would have to depend on.

        The last two are both fallbacks for a run nobody gave an id to.
        `databricks bundle run model_annealing` is exactly that — the job's
        `DBX_RUN_ID` parameter defaults to empty — and it is the first thing
        anyone does with a new job, so it is worth being deliberate about:
        `job-<databricks run id>` on a workspace, because a row that can be
        traced back to a run in the Jobs UI is worth far more than a row keyed
        `local` in a shared table; `local` only off a workspace, matching
        `run_local`'s own default so a laptop run's rows and its telemetry
        agree by construction.

        **A gap remains, and it is the harness's rather than this model's.**
        When `DBX_RUN_ID` is empty, `JobConfig` generates `run-<hex>` for the
        telemetry and never writes it back to the environment, so the rows and
        the telemetry are keyed differently. The `result` message's
        `fetch_hint` carries the id the rows actually got, so they stay
        findable either way. Closing it properly is one line in the harness or
        one field in `DBX_MODEL_CONFIG`, and both are platform decisions.
        """
        job_run = os.environ.get("DATABRICKS_JOB_RUN_ID", "").strip()
        return str(
            self.run_id
            or os.environ.get("DBX_RUN_ID")
            or (f"job-{job_run}" if job_run else "local")
        ).strip()

    def _load_problem(self) -> Problem:
        data = nyc_taxi_trips(limit=self.n_items, seed=self.data_seed).dropna(
            "fare_amount", "duration_min", "trip_distance"
        )
        rows = [
            row
            for row in data.rows[: self.n_items]
            if float(row["duration_min"]) > 0 and float(row["fare_amount"]) > 0
        ]

        # A loader that came back with nothing usable must not silently become
        # a knapsack over an empty list that then reports a triumphant zero.
        if not rows:
            self.log(
                "no usable trips in the dataset; the knapsack is empty",
                level="WARNING",
                phase="input",
            )

        values = tuple(float(row["fare_amount"]) for row in rows)
        weights = tuple(float(row["duration_min"]) for row in rows)
        distances = tuple(float(row["trip_distance"]) for row in rows)
        capacity = float(self.capacity_fraction) * math.fsum(weights)
        densities = [v / w for v, w in zip(values, weights, strict=True)]

        self.log(data.provenance, phase="input")
        if values:
            self.log(
                f"{len(values)} trips on offer, {math.fsum(values):.2f} in fares over "
                f"{math.fsum(weights):.0f} minutes; shift is {capacity:.0f} minutes "
                f"(mean fare {statistics.fmean(values):.2f})",
                phase="input",
            )

        return Problem(
            values=values,
            weights=weights,
            distances=distances,
            capacity=capacity,
            penalty_rate=PENALTY_FACTOR * (max(densities) if densities else 1.0),
            # Always present, even on real data, so the results table has one
            # schema wherever it ran.
            provenance=data.provenance,
            data_fields=data.describe(),
        )

    # --- the objective ------------------------------------------------------

    @property
    def problem(self) -> Problem:
        return self._problem

    def objective(self, value: float, weight: float) -> float:
        """Fares taken, less a per-minute charge for overrunning the shift.

        Defined on infeasible states too — that is what lets the search walk
        through them — but priced so no infeasible state can ever win.
        """
        problem = self._problem
        return value - problem.penalty_rate * max(0.0, weight - problem.capacity)

    def evaluate(self, selection: list[bool]) -> tuple[float, float, float]:
        """``(value, weight, objective)`` for a whole selection, from scratch.

        The search itself never calls this — it updates value and weight
        incrementally, which is what keeps an iteration in the microseconds.
        It exists so a caller can check that the incremental arithmetic has not
        drifted from the definition, which is the one property of this model
        that is not visible from its output.
        """
        problem = self._problem
        value = math.fsum(v for v, on in zip(problem.values, selection, strict=True) if on)
        weight = math.fsum(w for w, on in zip(problem.weights, selection, strict=True) if on)
        return value, weight, self.objective(value, weight)

    def temperature(self, iteration: int) -> float:
        """Geometric cooling from start to end over the planned iterations."""
        start, end = self._temperature_bounds()
        if self.iterations <= 1:
            return end
        return start * (end / start) ** (iteration / (self.iterations - 1))

    def _temperature_bounds(self) -> tuple[float, float]:
        problem = self._problem
        if self.start_temperature is not None:
            start = float(self.start_temperature)
        elif problem.values:
            start = START_TEMPERATURE_FACTOR * statistics.fmean(problem.values)
        else:
            start = 1.0
        start = max(start, 1e-9)
        end = (
            float(self.end_temperature)
            if self.end_temperature is not None
            else start * END_TEMPERATURE_RATIO
        )
        return start, max(end, 1e-12)

    # --- the search ---------------------------------------------------------

    def step(self, i: int) -> dict[str, Any]:
        """One sweep: `progress_every` proposed moves, then one progress point.

        The template does the emitting, the percentage and the cancel check
        between sweeps. What is left here is Metropolis.
        """
        problem = self._problem
        n = len(problem)
        values, weights = problem.values, problem.weights
        rng = self._rng
        selection, selected, unselected, where = (
            self._selection,
            self._selected,
            self._unselected,
            self._where,
        )
        swap_probability = float(self.swap_probability)

        first = self._iterations_run
        last = min(self.iterations, first + self.progress_every)
        accepted_window = 0
        attempted_window = 0
        # Seeded with the end temperature so the payload's `temperature` key is
        # present even for a sweep that runs no iterations. `prestep` sizes
        # things so that cannot happen; a payload whose keys depend on that
        # staying true is a worse bargain than one line here.
        temperature = self._temperature_bounds()[1]

        for iteration in range(first, last):
            temperature = self.temperature(iteration)

            swapping = bool(selected) and bool(unselected) and rng.random() < swap_probability
            if swapping:
                out_item = selected[rng.randrange(len(selected))]
                in_item = unselected[rng.randrange(len(unselected))]
                new_value = self._cur_value - values[out_item] + values[in_item]
                new_weight = self._cur_weight - weights[out_item] + weights[in_item]
            else:
                item = rng.randrange(n)
                sign = -1.0 if selection[item] else 1.0
                new_value = self._cur_value + sign * values[item]
                new_weight = self._cur_weight + sign * weights[item]

            new_obj = self.objective(new_value, new_weight)
            delta = new_obj - self._cur_obj
            attempted_window += 1

            # Uphill moves are accepted on purpose — that is the algorithm, and
            # the reason this model's current objective is non-monotonic.
            if delta >= 0.0 or rng.random() < math.exp(delta / temperature):
                accepted_window += 1
                self._accepted_total += 1
                if swapping:
                    selection[out_item] = False
                    selection[in_item] = True
                    _move(out_item, selected, unselected, where)
                    _move(in_item, unselected, selected, where)
                elif selection[item]:
                    selection[item] = False
                    _move(item, selected, unselected, where)
                else:
                    selection[item] = True
                    _move(item, unselected, selected, where)
                self._cur_value, self._cur_weight, self._cur_obj = new_value, new_weight, new_obj

                # The incumbent only ever moves on a *feasible* state, so
                # primary_metric stays both monotonic and honest.
                if self._cur_weight <= problem.capacity and self._cur_value > self.best_value:
                    self.best_value = self._cur_value
                    self.best_selection = list(selection)

        self._iterations_run = last

        return {
            # The BEST, not the current — monotonic, so a generic view that
            # knows nothing about annealing still plots something sensible.
            "metric": self.best_value,
            "label": "best_fare",
            # The non-monotonic half of the story, for a view that does know.
            "iteration": self._iterations_run,
            "iterations_total": self.iterations,
            "temperature": temperature,
            "current_objective": self._cur_obj,
            "current_value": self._cur_value,
            "current_weight": self._cur_weight,
            "capacity": problem.capacity,
            "feasible": self._cur_weight <= problem.capacity,
            "acceptance_rate": accepted_window / attempted_window if attempted_window else 0.0,
            "accepted_total": self._accepted_total,
            "items_selected": sum(self.best_selection),
        }

    # --- a baseline worth beating ------------------------------------------

    def random_baseline(self, trials: int = 200) -> float:
        """Best fare from ``trials`` shifts filled by taking trips at random.

        Not a straw man: random-greedy fill packs the shift right up to
        capacity every time, so it is already a decent knapsack heuristic. If
        the annealing cannot beat *this*, it is an expensive random number
        generator, and the result rows say so without anyone re-running it.

        Its own RNG, seeded off the search's, so scoring a baseline can never
        perturb the search itself.
        """
        problem = self._problem
        n = len(problem)
        if n == 0 or trials <= 0:
            return 0.0

        rng = random.Random(self.seed + 1)
        order = list(range(n))
        best = 0.0
        for _ in range(trials):
            rng.shuffle(order)
            value = weight = 0.0
            for item in order:
                if weight + problem.weights[item] <= problem.capacity:
                    weight += problem.weights[item]
                    value += problem.values[item]
            best = max(best, value)
        return best

    # --- results ------------------------------------------------------------

    def poststep(self, status: str) -> None:
        """Score the baseline, write the shift, report what was written.

        `poststep` runs on the cancelled and failed paths too, and for this
        model that is the whole reason it is where the write lives: **a
        cancelled annealing run keeps its incumbent.** Discarding a solution
        because someone pressed stop would be the wrong behaviour, and results
        are not best-effort.
        """
        self.baseline_value = self.random_baseline(int(self.baseline_trials))
        self.log(
            f"best feasible fare {self.best_value:.2f} from {sum(self.best_selection)} trips "
            f"after {self._iterations_run} iterations ({self._accepted_total} moves accepted); "
            f"random baseline {self.baseline_value:.2f}",
            phase="solve",
        )

        rows = self._result_rows(status)
        if not rows:
            self.log(
                "the search selected no trips; nothing to write",
                level="WARNING",
                phase="results",
            )
            self._report(0, rows, written=False, reason="the search selected no trips")
            return

        try:
            written = write_rows(self.target_table, RESULT_SCHEMA, rows)
        except Exception as exc:  # noqa: BLE001 - reported as a result, then re-raised
            self.log(
                f"writing {len(rows)} rows to {self.target_table} failed: {exc!r}",
                level="ERROR",
                phase="results",
            )
            self._report(0, rows, written=False, reason=f"{type(exc).__name__}: {exc}")
            # A run must not report SUCCEEDED over a lost result. It may still
            # report CANCELLED or FAILED over one: on the failed path the
            # exception that got us here is the more useful thing to surface,
            # and on the cancelled path a human already knows the run did not
            # finish. Both are recorded in the `result` message above either
            # way, with row_count 0 and the reason.
            if status == SUCCEEDED:
                raise
            return

        if written is None:
            self.log(
                f"no Spark session: {len(rows)} rows were NOT written to {self.target_table}",
                level="WARNING",
                phase="results",
            )
            self._report(0, rows, written=False, reason="no Spark session (nothing was written)")
            return

        self.log(f"wrote {written} rows to {self.target_table}", phase="results")
        self._report(written, rows, written=True, reason=None)

    def _report(
        self,
        row_count: int,
        rows: list[dict[str, Any]],
        *,
        written: bool,
        reason: str | None,
    ) -> None:
        """The `result` envelope: how many rows, where, and a preview.

        `_emit` rather than a `result()` helper because `modelkit.Model` has
        none — it has `log()` and `progress()`, which are the two every model
        wants, and adding a third to a shared library is a change to every
        model environment that this port does not need. It is the same callback
        those two use.

        The preview is built even when nothing was written: it is the one part
        of a result that survives having nowhere to put it, and a browser can
        still draw the shift the search found.
        """
        hint: dict[str, Any] = {
            "table": self.target_table,
            "key": "run_id",
            "run_id": self.run_id,
            "written": written,
        }
        if reason:
            hint["reason"] = reason

        self._emit(
            "result",
            # 0 is a real answer here and the field exists to carry it: it is
            # what distinguishes "wrote nothing" from "never got that far".
            row_count=row_count,
            fetch_hint=hint,
            preview=_preview(rows),
            # chunk_index=0 and final=True are the envelope's defaults and the
            # once-at-the-end case this model is. A chunked model says so.
        )

    def _result_rows(self, status: str) -> list[dict[str, Any]]:
        """The chosen shift: one row per trip taken, plus its solution.

        Every numeric is built as a float or an int deliberately: PySpark's row
        verifier is strict, and a DOUBLE column will not accept a Python int —
        a failure that happens on a workspace at the end of a run and nowhere
        else.
        """
        problem = self._problem
        chosen = [i for i, on in enumerate(self.best_selection) if on]
        if not chosen:
            return []

        weight = math.fsum(problem.weights[i] for i in chosen)
        baseline = self.baseline_value
        run_fields: dict[str, Any] = {
            "run_id": self.run_id,
            # Stamped here because nothing else does any more. This model emits
            # its results once, at the end, so there is exactly one chunk.
            "chunk_index": 0,
            # `objective` and `total_value` coincide by construction — the
            # incumbent is only ever updated on a feasible state, so its
            # penalty is zero. Both columns stay because they are the pair
            # every model's results table carries, and a model whose incumbent
            # could be infeasible would fill them differently.
            "objective": round(self.best_value, 6),
            "total_value": round(self.best_value, 6),
            "total_weight": round(weight, 6),
            "items_selected": len(chosen),
            "iterations_run": self._iterations_run,
            "iterations_planned": self.iterations,
            # What the MODEL observed, and `iterations_run` above is the
            # evidence for it. The harness has the last word on the run's
            # terminal status — a cancel that arrives while this method is
            # running lands there and not here.
            "cancelled": status == CANCELLED,
            "seed": int(self.seed),
            "baseline_objective": None if baseline is None else round(baseline, 6),
            "improvement_over_baseline_pct": _improvement_pct(self.best_value, baseline),
            **problem.data_fields,
            **problem.describe(),
        }

        # Ranked by value density so the preview curve reads as "the best
        # minutes of the shift first".
        chosen.sort(key=lambda i: problem.values[i] / problem.weights[i], reverse=True)
        return [
            {
                "rank": rank,
                "item_index": item,
                "value": round(problem.values[item], 6),
                "weight": round(problem.weights[item], 6),
                "distance": round(problem.distances[item], 6),
                "value_density": round(problem.values[item] / problem.weights[item], 6),
                **run_fields,
            }
            for rank, item in enumerate(chosen)
        ]


def _preview(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The value-density curve, bounded, from the rows that were written.

    Four keys rather than twenty-six: a preview is what a browser draws
    immediately, and the full row is one query away through `fetch_hint`.

    Evenly strided rather than LTTB, and that is safe HERE specifically: the
    rows are sorted by value density, so the curve is monotonically decreasing
    and a stride cannot hide a spike the way it can in a time series.
    `shared.downsample.lttb` is what the platform uses for time-series-shaped
    results, and this model does not import it — a model depends on nothing
    from this repo, which is what makes it movable.
    """
    points = [
        {
            "rank": row["rank"],
            "value_density": row["value_density"],
            "value": row["value"],
            "weight": row["weight"],
        }
        for row in rows
    ]
    if len(points) <= PREVIEW_POINTS:
        return points

    stride = math.ceil(len(points) / PREVIEW_POINTS)
    sampled = points[::stride]
    # Keep the last point: the tail of this curve is the marginal trip in the
    # shift, which is the interesting end of it.
    if sampled[-1] is not points[-1]:
        sampled.append(points[-1])
    return sampled


def _move(item: int, out_of: list[int], into: list[int], where: list[int]) -> None:
    """Move ``item`` between the selected and unselected lists in O(1).

    Swap-with-last-then-pop: the lists are unordered bags, so the order they
    end up in does not matter — but it must be *deterministic*, and it is,
    because the sequence of moves is.
    """
    position = where[item]
    last = out_of[-1]
    out_of[position] = last
    where[last] = position
    out_of.pop()
    where[item] = len(into)
    into.append(item)


def _improvement_pct(best: float, baseline: float | None) -> float | None:
    if baseline is None or baseline <= 0:
        return None
    return round(100.0 * (best - baseline) / baseline, 6)


def build_model(config: dict[str, Any] | None = None) -> Annealing:
    """Kept for callers that want a factory rather than the class.

    The entry point names `Annealing` directly — `modelkit.Model.__init__`
    already takes both a config dict and keywords, so a separate factory has
    nothing left to do. This remains because `build_model` is the first name
    `job/loader.py` looks for, and a model author reading that list should find
    the obvious thing working.
    """
    return Annealing(config or {})
