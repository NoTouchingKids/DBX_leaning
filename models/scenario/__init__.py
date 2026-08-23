"""Cheap, deterministic scenario sweep — the fan-out case."""

from .model import DEFAULT_GRID, Baseline, ScenarioModel, build_model

__all__ = ["ScenarioModel", "Baseline", "build_model", "DEFAULT_GRID"]
