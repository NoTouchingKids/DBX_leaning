"""Staff shift-scheduling MILP on Gurobi's bundled restricted licence.

Licence expiry is recorded in LICENCE_EXPIRY.md next to the pin — it fails
hard, not gracefully, once past its date.
"""

from .instance import SHIFTS, Instance, build_instance
from .model import SchedulingModel, build_model

__all__ = ["SchedulingModel", "build_model", "Instance", "build_instance", "SHIFTS"]
