"""Deterministic instance generation for the scheduling MILP.

Sized to fit the bundled restricted licence's 2000 variable / 2000 constraint
cap with real headroom — see LICENCE_EXPIRY.md. The size is asserted in tests,
not eyeballed here.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

__all__ = ["Instance", "build_instance", "SHIFTS"]

#: Order matters: NIGHT followed by MORNING is the rest-violation pair.
SHIFTS = ("morning", "evening", "night")


@dataclass(frozen=True)
class Instance:
    staff: tuple[str, ...]
    days: int
    shifts: tuple[str, ...] = SHIFTS
    #: (day, shift) -> how many people must be on
    demand: dict[tuple[int, str], int] = field(default_factory=dict)
    #: (staff, day) -> can they work at all that day
    available: dict[tuple[str, int], bool] = field(default_factory=dict)
    #: (staff, shift) -> relative cost of using them on that shift
    cost: dict[tuple[str, str], float] = field(default_factory=dict)
    #: (staff, shift) -> preference bonus, subtracted from cost
    preference: dict[tuple[str, str], float] = field(default_factory=dict)
    max_shifts_per_staff: int = 10

    @property
    def variable_count(self) -> int:
        return len(self.staff) * self.days * len(self.shifts)


def build_instance(
    *,
    staff_count: int = 20,
    days: int = 14,
    seed: int = 20260822,
    max_shifts_per_staff: int = 10,
    demand_per_shift: tuple[int, int, int] = (4, 3, 2),
) -> Instance:
    """A fixed instance for a given seed. Same seed, same problem, always."""
    rng = random.Random(seed)
    staff = tuple(f"staff-{i:02d}" for i in range(staff_count))

    demand = {
        (d, shift): demand_per_shift[k] for d in range(days) for k, shift in enumerate(SHIFTS)
    }
    # Roughly one person in ten is unavailable on any given day.
    available = {(s, d): rng.random() > 0.1 for s in staff for d in range(days)}
    cost = {(s, shift): 1.0 + rng.random() for s in staff for shift in SHIFTS}
    preference = {(s, shift): rng.choice((0.0, 0.0, 0.3)) for s in staff for shift in SHIFTS}

    return Instance(
        staff=staff,
        days=days,
        demand=demand,
        available=available,
        cost=cost,
        preference=preference,
        max_shifts_per_staff=max_shifts_per_staff,
    )
