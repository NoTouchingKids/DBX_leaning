"""Staff shift-scheduling MILP on Gurobi's bundled restricted licence.

Licence expiry is recorded in LICENCE_EXPIRY.md next to the pin — it fails
hard, not gracefully, once past its date.
"""

from .instance import SHIFT_HOURS, SHIFTS, Instance, build_instance, hourly_volumes, shift_of_hour
from .model import SchedulingModel, build_model

__all__ = [
    "SchedulingModel",
    "build_model",
    "Instance",
    "build_instance",
    "hourly_volumes",
    "shift_of_hour",
    "SHIFTS",
    "SHIFT_HOURS",
]
