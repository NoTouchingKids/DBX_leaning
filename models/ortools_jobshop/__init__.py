"""Job-shop scheduling on OR-Tools CP-SAT — the open-source counterweight.

The solver is ``ortools.sat.python.cp_model`` (CP-SAT, constraint programming),
never ``ortools.linear_solver.pywraplp`` (the legacy MPSolver wrapper). See
``model.py`` for why that distinction is the point of this package, and
``instance.py`` for how a bakehouse sales table becomes a shop floor.

No licence file, no expiry date, no token endpoint to reach, and no variable or
constraint cap — the three things the two Gurobi models each have to work
around.
"""

from .instance import (
    MAX_JOBS,
    RECIPES,
    SALES_TABLE,
    STAGES,
    Instance,
    Job,
    Operation,
    bakery_batches,
    build_instance,
    operation_ceiling_for,
    recipe_for,
)
from .model import DEFAULT_TIME_LIMIT_S, JobShopModel, build_model

__all__ = [
    "JobShopModel",
    "build_model",
    "Instance",
    "Job",
    "Operation",
    "build_instance",
    "bakery_batches",
    "recipe_for",
    "operation_ceiling_for",
    "STAGES",
    "RECIPES",
    "MAX_JOBS",
    "SALES_TABLE",
    "DEFAULT_TIME_LIMIT_S",
]
