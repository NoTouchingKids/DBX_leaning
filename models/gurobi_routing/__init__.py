"""Capacitated vehicle routing with lazy subtour-elimination constraints.

The model that exercises the harness's callback *composition*: it supplies its
own ``gurobi_callback`` (rounded-capacity/subtour cuts via ``cbLazy``) and the
harness runs it alongside its own log, progress and cancellation observers in
Gurobi's single callback slot. See ``model.py`` for why the cuts cannot simply
be written into the model, and ``job/drivers/gurobi.py`` for the composition.

Licence: the bundled restricted licence, 2000 variables / 2000 constraints,
no WLS. The pin and its expiry date live in
``models/gurobi_scheduling/LICENCE_EXPIRY.md`` — one copy, deliberately, so
the two Gurobi models cannot disagree about it.
"""

from .instance import MAX_STOPS, Instance, Stop, build_instance, variable_count_for
from .model import DEFAULT_TIME_LIMIT_S, RoutingModel, build_model

__all__ = [
    "RoutingModel",
    "build_model",
    "Instance",
    "Stop",
    "build_instance",
    "variable_count_for",
    "MAX_STOPS",
    "DEFAULT_TIME_LIMIT_S",
]
