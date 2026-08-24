"""Simulated annealing over a knapsack — the zero-dependency control case.

**Why this model exists.** Every other model here pulls in a library:
gurobipy, scikit-learn, emcee, numpy. This one pulls in nothing. It is the
control case for the microservice split in `CLAUDE.md` — the evidence that a
job really can deploy with the harness's own transport and nothing else, and
still be a real model rather than a smoke test. Its extra in
`[project.optional-dependencies]` is an empty list, deliberately. `random`,
`math` and `statistics` from the standard library are the whole toolbox.

(`models._data` is imported for problem inputs. It is stdlib-only itself, so
the claim stays true of the deployed environment; it is a claim about
third-party packages, not about zero imports.)

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
catalog (`models._data`) — value is the fare, weight is the trip's duration —
or that loader's deterministic fallback when there is no workspace. Weights
and values that correlate the way real ones do is what makes this a search
worth running; a knapsack over uniform random numbers is solved by greed.

**Determinism.** A seeded ``random.Random``, never the module-level ``random``
— that global is shared process-wide, so anything else in the job touching it
would change this run's answer. Same seed, same trips, same solution, every
time. A stochastic search that cannot be reproduced cannot be debugged.
"""

from __future__ import annotations

import math
import random
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from models._data import nyc_taxi_trips

__all__ = ["AnnealingModel", "Problem", "build_model"]

#: How many trips are on offer. Small enough that a pure-Python sweep of
#: thousands of iterations finishes in a second or two, large enough that the
#: search space (2**240) is not enumerable and the annealing is doing real work.
DEFAULT_ITEMS = 240
#: Shift length as a share of the total duration on offer. A knapsack is only
#: interesting when it binds: at 1.0 you take everything, at 0.01 you take the
#: single best trip. A quarter leaves a genuinely combinatorial choice.
DEFAULT_CAPACITY_FRACTION = 0.25
DEFAULT_ITERATIONS = 30_000
#: Starting temperature as a multiple of the mean fare: a move that costs
#: about one average fare is accepted roughly 37% of the time at the start.
#: Derived from the data, not a magic constant, so it still makes sense if the
#: loader hands over a different fare distribution.
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


class AnnealingModel:
    results_table = "results_annealing"
    #: Selected trips, ranked by value density — a decreasing curve, which is
    #: what LTTB downsampling is good at previewing.
    preview_axes = ("rank", "value_density")

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = dict(config or {})
        self.iterations = int(cfg.get("iterations", DEFAULT_ITERATIONS))
        self.seed = int(cfg.get("seed", 20_260_823))
        self.swap_probability = float(cfg.get("swap_probability", DEFAULT_SWAP_PROBABILITY))
        self.capacity_fraction = float(cfg.get("capacity_fraction", DEFAULT_CAPACITY_FRACTION))
        #: Thousands of iterations at microseconds each: a message per
        #: iteration would flood the channel and tell a reader nothing a
        #: sampled curve does not. Batched by count *or* by wall clock,
        #: whichever comes first, so a slow instance still reports.
        self.progress_every = int(cfg.get("progress_every", 1_000))
        self.progress_every_s = float(cfg.get("progress_every_s", 1.0))
        #: How many random-greedy shifts to score for comparison. This is the
        #: number that answers "is this search worth its iterations, or an
        #: expensive random number generator?" — so it is telemetry, not a
        #: test fixture, and it rides on the result rows.
        self.baseline_trials = int(cfg.get("baseline_trials", 200))
        #: How much of the trip table to offer.
        self.n_items = int(cfg.get("n_items", DEFAULT_ITEMS))
        self.data_seed = int(cfg.get("data_seed", 11))
        #: Absolute overrides; None means "derive it from the data".
        self._start_temperature = _optional_float(cfg.get("start_temperature"))
        self._end_temperature = _optional_float(cfg.get("end_temperature"))

        self.emit: Callable[..., Any] | None = None
        self.should_cancel: Callable[[], bool] | None = None

        self._problem: Problem | None = None
        #: The incumbent: the best *feasible* selection seen, and its value.
        self.best_selection: list[bool] = []
        self.best_value: float = 0.0
        self.iterations_run: int = 0
        self.cancelled: bool = False
        self.baseline_value: float | None = None

    # --- the instance -----------------------------------------------------

    def build(self) -> None:
        """Load the trips once, up front. Idempotent."""
        self._ensure_problem()

    @property
    def problem(self) -> Problem:
        return self._ensure_problem()

    def _ensure_problem(self) -> Problem:
        if self._problem is not None:
            return self._problem

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
            self._log(
                "no usable trips in the dataset; the knapsack is empty",
                phase="input",
                level="WARNING",
            )

        values = tuple(float(row["fare_amount"]) for row in rows)
        weights = tuple(float(row["duration_min"]) for row in rows)
        distances = tuple(float(row["trip_distance"]) for row in rows)
        capacity = self.capacity_fraction * math.fsum(weights)
        densities = [v / w for v, w in zip(values, weights, strict=True)]

        self._problem = Problem(
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

        self._log(self._problem.provenance, phase="input")
        if values:
            self._log(
                f"{len(values)} trips on offer, {math.fsum(values):.2f} in fares over "
                f"{math.fsum(weights):.0f} minutes; shift is {capacity:.0f} minutes "
                f"(mean fare {statistics.fmean(values):.2f})",
                phase="input",
            )
        return self._problem

    # --- the objective ----------------------------------------------------

    def objective(self, value: float, weight: float) -> float:
        """Fares taken, less a per-minute charge for overrunning the shift.

        Defined on infeasible states too — that is what lets the search walk
        through them — but priced so no infeasible state can ever win.
        """
        return value - self.problem.penalty_rate * max(0.0, weight - self.problem.capacity)

    def evaluate(self, selection: list[bool]) -> tuple[float, float, float]:
        """``(value, weight, objective)`` for a whole selection, from scratch.

        The search itself never calls this — it updates value and weight
        incrementally, which is what keeps an iteration in the microseconds.
        It exists so a caller (or a test) can check that the incremental
        arithmetic has not drifted from the definition.
        """
        problem = self.problem
        value = math.fsum(v for v, on in zip(problem.values, selection, strict=True) if on)
        weight = math.fsum(w for w, on in zip(problem.weights, selection, strict=True) if on)
        return value, weight, self.objective(value, weight)

    # --- the search -------------------------------------------------------

    def temperature(self, iteration: int) -> float:
        """Geometric cooling from start to end over the planned iterations."""
        start, end = self._temperature_bounds()
        if self.iterations <= 1:
            return end
        return start * (end / start) ** (iteration / (self.iterations - 1))

    def _temperature_bounds(self) -> tuple[float, float]:
        problem = self.problem
        if self._start_temperature is not None:
            start = self._start_temperature
        elif problem.values:
            start = START_TEMPERATURE_FACTOR * statistics.fmean(problem.values)
        else:
            start = 1.0
        start = max(start, 1e-9)
        end = self._end_temperature if self._end_temperature is not None else (
            start * END_TEMPERATURE_RATIO
        )
        return start, max(end, 1e-12)

    def run(self) -> None:
        problem = self._ensure_problem()
        n = len(problem)
        rng = random.Random(self.seed)  # never the module-level random

        self.best_selection = [False] * n
        self.best_value = 0.0
        self.iterations_run = 0
        self.cancelled = False

        if n == 0:
            self._log("nothing to search", phase="solve", level="WARNING")
            return

        values, weights = problem.values, problem.weights
        selection = [False] * n
        selected: list[int] = []
        unselected = list(range(n))
        #: Position of each item in whichever of the two lists holds it, so a
        #: swap is O(1) rather than a list scan — the difference between an
        #: iteration costing microseconds and costing milliseconds.
        where = list(range(n))

        cur_value = 0.0
        cur_weight = 0.0
        cur_obj = 0.0
        swap_probability = self.swap_probability

        started = time.monotonic()
        last_emit = started
        accepted_window = 0
        attempted_window = 0
        accepted_total = 0

        self._log(
            f"annealing {self.iterations} iterations over {n} trips, seed {self.seed}",
            phase="solve",
        )

        for iteration in range(self.iterations):
            # Between iterations, batched: each one is microseconds, so
            # checking every single time would cost more than the search.
            if iteration % self.progress_every == 0 and self._cancelled():
                self.cancelled = True
                self._log(
                    f"cancelled after {iteration} of {self.iterations} iterations; "
                    f"keeping the best solution found so far",
                    phase="solve",
                )
                break

            temperature = self.temperature(iteration)

            swapping = bool(selected) and bool(unselected) and rng.random() < swap_probability
            if swapping:
                out_pos = rng.randrange(len(selected))
                in_pos = rng.randrange(len(unselected))
                out_item = selected[out_pos]
                in_item = unselected[in_pos]
                new_value = cur_value - values[out_item] + values[in_item]
                new_weight = cur_weight - weights[out_item] + weights[in_item]
            else:
                item = rng.randrange(n)
                sign = -1.0 if selection[item] else 1.0
                new_value = cur_value + sign * values[item]
                new_weight = cur_weight + sign * weights[item]

            new_obj = self.objective(new_value, new_weight)
            delta = new_obj - cur_obj
            attempted_window += 1

            # Uphill moves are accepted on purpose — that is the algorithm,
            # and the reason this model's current objective is non-monotonic.
            if delta >= 0.0 or rng.random() < math.exp(delta / temperature):
                accepted_window += 1
                accepted_total += 1
                if swapping:
                    selection[out_item] = False
                    selection[in_item] = True
                    _move(out_item, selected, unselected, where)
                    _move(in_item, unselected, selected, where)
                else:
                    if selection[item]:
                        selection[item] = False
                        _move(item, selected, unselected, where)
                    else:
                        selection[item] = True
                        _move(item, unselected, selected, where)
                cur_value, cur_weight, cur_obj = new_value, new_weight, new_obj

                # The incumbent only ever moves on a *feasible* state, so
                # primary_metric stays both monotonic and honest.
                if cur_weight <= problem.capacity and cur_value > self.best_value:
                    self.best_value = cur_value
                    self.best_selection = list(selection)

            self.iterations_run = iteration + 1

            now = time.monotonic()
            batched = self.iterations_run % self.progress_every == 0
            # The last iteration always reports, so percent_complete lands on
            # 100 even when the batch size does not divide the iteration count
            # — a curve that stops at 97% reads as a run that died.
            finished = self.iterations_run == self.iterations
            if batched or finished or (now - last_emit) >= self.progress_every_s:
                last_emit = now
                self._progress(
                    now - started,
                    temperature,
                    cur_obj,
                    cur_value,
                    cur_weight,
                    accepted_window / attempted_window if attempted_window else 0.0,
                    accepted_total,
                )
                accepted_window = attempted_window = 0

        self.baseline_value = self.random_baseline(self.baseline_trials)
        self._log(
            f"best feasible fare {self.best_value:.2f} from "
            f"{sum(self.best_selection)} trips after {self.iterations_run} iterations "
            f"({accepted_total} moves accepted); "
            f"random baseline {self.baseline_value:.2f}",
            phase="solve",
        )

    def _cancelled(self) -> bool:
        return self.should_cancel is not None and self.should_cancel()

    # --- a baseline worth beating ----------------------------------------

    def random_baseline(self, trials: int = 200) -> float:
        """Best fare from ``trials`` shifts filled by taking trips at random.

        Not a straw man: random-greedy fill packs the shift right up to
        capacity every time, so it is already a decent knapsack heuristic. If
        the annealing cannot beat *this*, it is an expensive random number
        generator and `tests/models/test_annealing.py` says so.

        Its own RNG, seeded off the search's, so scoring a baseline can never
        perturb the search itself.
        """
        problem = self.problem
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

    # --- results ----------------------------------------------------------

    def results(self) -> list[dict[str, Any]]:
        """The chosen shift: one row per trip taken, plus its objective.

        A cancelled run returns the best solution found before the stop, not
        nothing — results are not best-effort.
        """
        if not self.best_selection:
            return []

        problem = self.problem
        chosen = [i for i, on in enumerate(self.best_selection) if on]
        weight = math.fsum(problem.weights[i] for i in chosen)
        baseline = self.baseline_value
        run_fields = {
            "objective": round(self.best_value, 6),
            "total_value": round(self.best_value, 6),
            "total_weight": round(weight, 6),
            "items_selected": len(chosen),
            "iterations_run": self.iterations_run,
            "iterations_planned": self.iterations,
            "seed": self.seed,
            "cancelled": self.cancelled,
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

    # --- telemetry --------------------------------------------------------

    def _progress(
        self,
        elapsed: float,
        temperature: float,
        current_objective: float,
        current_value: float,
        current_weight: float,
        acceptance_rate: float,
        accepted_total: int,
    ) -> None:
        if self.emit is None:
            return
        problem = self.problem
        self.emit(
            "progress",
            elapsed_seconds=elapsed,
            # Iterations are the whole plan, so this is genuinely knowable.
            percent_complete=(
                100.0 * self.iterations_run / self.iterations if self.iterations else 100.0
            ),
            # The BEST, not the current — monotonic, so a generic view that
            # knows nothing about annealing still plots something sensible.
            primary_metric=self.best_value,
            primary_metric_label="best_fare",
            # The non-monotonic half of the story, for a view that does know.
            payload={
                "iteration": self.iterations_run,
                "iterations_total": self.iterations,
                "temperature": temperature,
                "current_objective": current_objective,
                "current_value": current_value,
                "current_weight": current_weight,
                "capacity": problem.capacity,
                "feasible": current_weight <= problem.capacity,
                "acceptance_rate": acceptance_rate,
                "accepted_total": accepted_total,
                "items_selected": sum(self.best_selection),
            },
        )

    def _log(self, message: str, *, phase: str = "solve", level: str = "INFO") -> None:
        if self.emit is not None:
            self.emit("log", message=message, source="model", phase=phase, level=level)


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


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def build_model(config: dict[str, Any] | None = None) -> AnnealingModel:
    return AnnealingModel(config)
