"""Execution drivers, selected by *capability*, not by model name.

The harness has no model-specific logic — no ``if model_type ==`` anywhere.
But it cannot drive every model the same way either: a Gurobi model must have
``optimize()`` called *by the harness*, so the harness can attach its own
progress/log/cancellation observers to the single callback slot Gurobi
allows. A model that drives itself just gets called.

So: two drivers, picked by what the discovered object actually exposes. A
model that hands over a ``gurobipy.Model`` gets the Gurobi driver; anything
with a ``run()``-shaped method drives itself. Adding a third solver family
later means adding a driver here, not branching in the runner.
"""

from __future__ import annotations

from .base import Driver, DriverResult
from .gurobi import GurobiDriver
from .self_driving import SelfDrivingDriver

__all__ = ["Driver", "DriverResult", "GurobiDriver", "SelfDrivingDriver", "select_driver"]


def select_driver(handle, emit, should_cancel, **options) -> Driver:
    from ..loader import ModelHandle  # local import keeps this module import-light

    assert isinstance(handle, ModelHandle)
    if handle.gurobi_model is not None:
        return GurobiDriver(handle, emit, should_cancel, **options)
    if handle.run is None:
        # Discovery accepted the model on the *presence* of the attribute; by
        # now build() has run and it should hold a real model.
        raise RuntimeError(
            f"{handle.spec} exposes `{handle.gurobi_model_attr}` but it is still None "
            f"after build() — the harness has nothing to solve. Build the gurobipy "
            f"model in build() (or __init__) and assign it to that attribute."
        )
    return SelfDrivingDriver(handle, emit, should_cancel)
