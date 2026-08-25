"""A deterministic scenario sweep, anchored on observed demand.

Every other model here is one long-running process per run. This one is
deliberately the opposite: cheap, fast, and numerous. Its job is to exercise
fan-out against Free Edition's ceiling of **5 concurrent job tasks per
account** — a case none of the others reach, because each of them is built to
be one long thing.

The grid is still multipliers, and still configurable. What it multiplies is
no longer arbitrary: the baseline demand, capacity and unit cost come from
Databricks' `samples` catalog (hourly NYC taxi trips) via `models._data`, or
from that loader's deterministic fallback when there is no workspace. So
"demand 1.2" means 20% above a real observed mean, not 20% above 1.

Determinism is the point, not an accident: same inputs, same outputs, every
time. The fallback loader is deterministic for a seed, so this holds offline
too. Do not add jitter, wall-clock, or unseeded randomness here.

The data is loaded **once**, in ``build()`` (or lazily on first use), never
per scenario — per-scenario work stays in the microseconds, which is the
whole reason this model is in the lineup.
"""

from __future__ import annotations

import itertools
import math
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import numpy as np

from .._data import nyc_taxi_hourly

__all__ = ["ScenarioModel", "Baseline", "build_model", "DEFAULT_GRID"]

#: Multipliers applied to the observed baseline. 6 x 4 x 3 = 72 scenarios.
#:
#: Demand spans a quiet hour to rather busier than the observed peak — hourly
#: taxi demand peaks at roughly twice its mean, so a grid that stopped at 1.3x
#: would sit entirely under a capacity sized on the 90th percentile and never
#: price a single shortfall. The span is what makes the sweep informative.
DEFAULT_GRID: dict[str, list[float]] = {
    "demand": [0.8, 1.0, 1.2, 1.5, 1.8, 2.1],
    "capacity": [0.9, 1.0, 1.1, 1.2],
    "unit_cost": [0.9, 1.0, 1.1],
}

#: A lost unit of demand costs its fare and then some — forgone revenue plus
#: the cost of turning a customer away. Multiple of the observed unit cost.
SHORTFALL_PENALTY_FACTOR = 2.0
#: Idle capacity is not free either: a share of unit cost per idle unit-hour.
IDLE_COST_FACTOR = 0.15
#: What a fleet actually gets sized for is not the mean hour, it is a busy
#: one. Percentile of observed hourly demand used as the capacity baseline.
CAPACITY_PERCENTILE = 90.0


@dataclass(frozen=True)
class Baseline:
    """The observed numbers the sweep varies around."""

    demand: float  # mean hourly trips
    peak_demand: float  # busiest observed hour
    capacity: float  # p90 hourly trips — what you would size for
    unit_cost: float  # mean fare per trip
    shortfall_penalty: float
    idle_cost: float
    #: One line for a log message, straight from the loader.
    provenance: str
    #: data_source / data_synthetic / data_rows / data_fallback_reason.
    data_fields: dict[str, Any]

    def describe(self) -> dict[str, Any]:
        return {
            "baseline_demand": round(self.demand, 6),
            "baseline_peak_demand": round(self.peak_demand, 6),
            "baseline_capacity": round(self.capacity, 6),
            "baseline_unit_cost": round(self.unit_cost, 6),
        }


class ScenarioModel:
    results_table = "results_scenario"
    preview_axes = ("scenario_index", "objective")

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = dict(config or {})
        self.grid: dict[str, list[float]] = cfg.get("grid") or DEFAULT_GRID
        #: Batch progress rather than emitting per scenario — at milliseconds
        #: per scenario, one message each would flood the channel.
        self.progress_every = int(cfg.get("progress_every", 10))
        self.progress_every_s = float(cfg.get("progress_every_s", 1.0))
        #: How much observed history the baseline is drawn from.
        self.data_days = int(cfg.get("data_days", 30))
        self.data_seed = int(cfg.get("data_seed", 7))
        #: Absolute overrides. None means "derive it from the observed data",
        #: which is the point of this model reading real numbers at all.
        self._shortfall_penalty = _optional_float(cfg.get("shortfall_penalty"))
        self._idle_cost = _optional_float(cfg.get("idle_cost"))

        self.emit: Callable[..., Any] | None = None
        self.should_cancel: Callable[[], bool] | None = None
        self.rows: list[dict[str, Any]] = []
        self._baseline: Baseline | None = None

    # --- the observed baseline -------------------------------------------

    def build(self) -> None:
        """Load the data once, up front. Idempotent."""
        self._ensure_baseline()

    @property
    def baseline(self) -> Baseline:
        """The observed baseline, loaded on first use and then cached."""
        return self._ensure_baseline()

    def _ensure_baseline(self) -> Baseline:
        if self._baseline is not None:
            return self._baseline

        data = nyc_taxi_hourly(days=self.data_days, seed=self.data_seed)
        trips = _finite(data.column("trips"))
        fares = _finite(data.column("avg_fare"))

        # A loader that came back with nothing usable must not silently become
        # a sweep around zero. Neutral units, and say so loudly.
        if trips.size == 0 or fares.size == 0:
            self._log(
                "no usable rows in the demand dataset; sweeping around unit baselines",
                phase="input",
                level="WARNING",
            )
            mean_demand = peak_demand = capacity = unit_cost = 1.0
        else:
            mean_demand = float(trips.mean())
            peak_demand = float(trips.max())
            capacity = float(np.percentile(trips, CAPACITY_PERCENTILE))
            unit_cost = float(fares.mean())

        self._baseline = Baseline(
            demand=mean_demand,
            peak_demand=peak_demand,
            capacity=capacity,
            unit_cost=unit_cost,
            shortfall_penalty=(
                self._shortfall_penalty
                if self._shortfall_penalty is not None
                else SHORTFALL_PENALTY_FACTOR * unit_cost
            ),
            idle_cost=(
                self._idle_cost if self._idle_cost is not None else IDLE_COST_FACTOR * unit_cost
            ),
            # Always present, even on real data, so the results table has one
            # schema wherever it ran.
            provenance=data.provenance,
            data_fields=data.describe(),
        )

        self._log(self._baseline.provenance, phase="input")
        self._log(
            f"baseline demand {mean_demand:.1f}/h (peak {peak_demand:.0f}), "
            f"capacity {capacity:.1f}, unit cost {unit_cost:.2f}",
            phase="input",
        )
        return self._baseline

    # --- the sweep --------------------------------------------------------

    def scenarios(self) -> Iterator[dict[str, float]]:
        names = sorted(self.grid)
        for combo in itertools.product(*(self.grid[n] for n in names)):
            yield dict(zip(names, combo, strict=True))

    @property
    def total(self) -> int:
        return math.prod(len(v) for v in self.grid.values()) if self.grid else 0

    def evaluate(self, scenario: dict[str, float]) -> dict[str, float]:
        """One scenario. Deterministic, closed-form, microseconds.

        ``scenario`` holds multipliers; the absolute quantities come from the
        observed baseline and are returned alongside the outcome so a result
        row records what was actually modelled.
        """
        base = self.baseline
        demand = base.demand * float(scenario["demand"])
        capacity = base.capacity * float(scenario["capacity"])
        unit_cost = base.unit_cost * float(scenario["unit_cost"])

        served = min(demand, capacity)
        shortfall = max(0.0, demand - capacity)
        idle = max(0.0, capacity - demand)
        objective = served * unit_cost - shortfall * base.shortfall_penalty - idle * base.idle_cost
        return {
            "demand": round(demand, 6),
            "capacity": round(capacity, 6),
            "unit_cost": round(unit_cost, 6),
            "served": round(served, 6),
            "shortfall": round(shortfall, 6),
            "idle": round(idle, 6),
            "objective": round(objective, 6),
        }

    def run(self) -> None:
        base = self._ensure_baseline()
        # Built once, then copied into every row — a run on real data and one
        # that fell back must be distinguishable afterwards, from the results
        # alone.
        provenance_fields = {**base.data_fields, **base.describe()}

        total = self.total
        started = time.monotonic()
        best: float | None = None
        last_emit = started
        self._log(f"sweeping {total} scenarios", phase="input")

        for index, scenario in enumerate(self.scenarios()):
            # Between scenarios, not mid-scenario: each one is microseconds.
            if self.should_cancel is not None and self.should_cancel():
                self._log(f"cancelled after {index} of {total} scenarios", phase="run")
                break

            outcome = self.evaluate(scenario)
            best = outcome["objective"] if best is None else max(best, outcome["objective"])
            self.rows.append(
                {
                    "scenario_index": index,
                    **{f"{name}_multiplier": value for name, value in scenario.items()},
                    **outcome,
                    **provenance_fields,
                }
            )

            now = time.monotonic()
            batched = (index + 1) % self.progress_every == 0
            due = batched or (now - last_emit) >= self.progress_every_s
            if due or index == total - 1:
                last_emit = now
                self._progress(index + 1, total, now - started, best, scenario, outcome)

    def results(self) -> list[dict[str, Any]]:
        """Whatever has been evaluated — a cancelled sweep keeps its scenarios."""
        return list(self.rows)

    # --- telemetry --------------------------------------------------------

    def _progress(self, done, total, elapsed, best, scenario, outcome) -> None:
        if self.emit is None:
            return
        self.emit(
            "progress",
            elapsed_seconds=elapsed,
            # Genuinely knowable here, unlike a MIP gap. This is the model
            # where percent_complete earns its place in the envelope.
            percent_complete=100.0 * done / total if total else 100.0,
            primary_metric=best,
            primary_metric_label="best_objective",
            payload={
                "scenarios_done": done,
                "scenarios_total": total,
                "last_scenario": scenario,
                "last_outcome": outcome,
            },
        )

    def _log(self, message: str, *, phase: str = "run", level: str = "INFO") -> None:
        if self.emit is not None:
            self.emit("log", message=message, source="model", phase=phase, level=level)


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _finite(values: list[Any]) -> np.ndarray:
    """Column to floats, dropping nulls and NaNs.

    The real table can return a null average for an hour; a baseline of NaN
    would poison every scenario silently, which is worse than one fewer hour.
    """
    numbers = np.asarray([float(v) for v in values if v is not None], dtype=float)
    return numbers[np.isfinite(numbers)]


def build_model(config: dict[str, Any] | None = None) -> ScenarioModel:
    return ScenarioModel(config)
