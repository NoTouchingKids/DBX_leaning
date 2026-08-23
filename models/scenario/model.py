"""A deterministic scenario sweep.

Every other model here is one long-running process per run. This one is
deliberately the opposite: cheap, fast, and numerous. Its job is to exercise
fan-out against Free Edition's ceiling of **5 concurrent job tasks per
account** — a case none of the others reach, because each of them is built to
be one long thing.

Determinism is the point, not an accident: same inputs, same outputs, every
time. Do not add jitter, wall-clock, or unseeded randomness here.
"""

from __future__ import annotations

import itertools
import math
import time
from collections.abc import Callable, Iterator
from typing import Any

__all__ = ["ScenarioModel", "build_model", "DEFAULT_GRID"]

DEFAULT_GRID: dict[str, list[float]] = {
    "demand": [0.8, 0.9, 1.0, 1.1, 1.2, 1.3],
    "capacity": [0.9, 1.0, 1.1, 1.2],
    "unit_cost": [10.0, 12.5, 15.0],
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
        self.shortfall_penalty = float(cfg.get("shortfall_penalty", 40.0))

        self.emit: Callable[..., Any] | None = None
        self.should_cancel: Callable[[], bool] | None = None
        self.rows: list[dict[str, Any]] = []

    # --- the sweep --------------------------------------------------------

    def scenarios(self) -> Iterator[dict[str, float]]:
        names = sorted(self.grid)
        for combo in itertools.product(*(self.grid[n] for n in names)):
            yield dict(zip(names, combo, strict=True))

    @property
    def total(self) -> int:
        return math.prod(len(v) for v in self.grid.values()) if self.grid else 0

    def evaluate(self, scenario: dict[str, float]) -> dict[str, float]:
        """One scenario. Deterministic, closed-form, milliseconds."""
        demand = scenario["demand"]
        capacity = scenario["capacity"]
        unit_cost = scenario["unit_cost"]

        served = min(demand, capacity)
        shortfall = max(0.0, demand - capacity)
        idle = max(0.0, capacity - demand)
        objective = served * unit_cost - shortfall * self.shortfall_penalty - idle * 2.0
        return {
            "served": round(served, 6),
            "shortfall": round(shortfall, 6),
            "idle": round(idle, 6),
            "objective": round(objective, 6),
        }

    def run(self) -> None:
        total = self.total
        started = time.monotonic()
        best: float | None = None
        last_emit = started
        self._log(f"sweeping {total} scenarios", phase="input")

        for index, scenario in enumerate(self.scenarios()):
            # Between scenarios, not mid-scenario: each one is milliseconds.
            if self.should_cancel is not None and self.should_cancel():
                self._log(f"cancelled after {index} of {total} scenarios", phase="run")
                break

            outcome = self.evaluate(scenario)
            best = outcome["objective"] if best is None else max(best, outcome["objective"])
            self.rows.append({"scenario_index": index, **scenario, **outcome})

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
            payload={"scenarios_done": done, "scenarios_total": total,
                     "last_scenario": scenario, "last_outcome": outcome},
        )

    def _log(self, message: str, *, phase: str = "run") -> None:
        if self.emit is not None:
            self.emit("log", message=message, source="model", phase=phase)


def build_model(config: dict[str, Any] | None = None) -> ScenarioModel:
    return ScenarioModel(config)
