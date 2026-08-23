"""Deterministic instance generation for the scheduling MILP.

Coverage requirements come from a **real demand curve** — hourly NYC taxi trip
volumes out of Databricks' free ``samples`` catalog, bucketed into the three
shifts. Staffing a real demand curve is what a shift-scheduling MILP is
actually for; a flat ``(4, 3, 2)`` requirement makes the coverage constraints
decorative. Availability, cost and preference stay generated — it is the
demand curve that benefits from being real.

Off a workspace, ``models._data`` falls back to a deterministic synthetic
curve of the same shape, so this module runs standalone and its tests do not
need Databricks. Which of the two happened is recorded on the ``Instance``
(``provenance`` / ``data_meta``) and carried into the result rows, so a run on
real data and a run that fell back are distinguishable afterwards.

Sized to fit the bundled restricted licence's 2000 variable / 2000 constraint
cap with real headroom — see LICENCE_EXPIRY.md. The size is asserted in tests,
not eyeballed here. Note that demand values do not change the model's *size*:
they are right-hand sides on coverage constraints that exist regardless. What
real demand can do is make the instance infeasible, so it is fitted to the
workforce that exists rather than being allowed to imply a bigger one — see
``_fit_to_capacity``.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from models._data import Dataset, nyc_taxi_hourly

__all__ = [
    "Instance",
    "build_instance",
    "SHIFTS",
    "SHIFT_HOURS",
    "shift_of_hour",
    "hourly_volumes",
]

#: Order matters: NIGHT followed by MORNING is the rest-violation pair.
SHIFTS = ("morning", "evening", "night")

#: [start, end) hour-of-day per shift. ``night`` wraps midnight; the small
#: hours are attributed to the calendar day they occur in, which keeps the
#: bucketing a pure function of the timestamp.
SHIFT_HOURS: dict[str, tuple[int, int]] = {
    "morning": (6, 14),
    "evening": (14, 22),
    "night": (22, 6),
}

_MS_PER_HOUR = 3_600_000
_MS_PER_DAY = 86_400_000

#: Average fraction of the workforce on duty on a given day. 0.45 x 20 staff
#: reproduces the (4, 3, 2) = 9 per day the hand-written instance used, so the
#: real curve is calibrated onto a workload the workforce can actually absorb.
TARGET_UTILISATION = 0.45

#: Hard ceilings used to fit demand to capacity (see ``_fit_to_capacity``).
#: Fractions of *actually available* staff on the day, and of the total
#: shift budget over the window.
DAY_UTILISATION_CEILING = 0.8
SHIFT_UTILISATION_CEILING = 0.4
WINDOW_UTILISATION_CEILING = 0.7

#: Every shift needs at least someone on it, however quiet the data says it is.
MIN_DEMAND = 1

#: How many of a calendar day's 24 hours must be present for it to count as a
#: whole day of demand. Not 24: the real table drops hours with no trips at
#: all, and rejecting a day over one dead 4am hour throws away good data.
MIN_HOURS_PER_DAY = 20

#: Slack days asked of the loader, so the partial first/last calendar day of
#: the window can be dropped without the curve having to cycle.
CURVE_SLACK_DAYS = 2


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
    #: One line for a log message at the ``input`` phase.
    provenance: str = "synthetic demand (no dataset read)"
    #: ``Dataset.describe()`` fields, carried into every result row.
    data_meta: dict[str, Any] = field(default_factory=dict)
    #: How the demand curve was turned into headcount, for the record.
    demand_meta: dict[str, Any] = field(default_factory=dict)

    @property
    def variable_count(self) -> int:
        return len(self.staff) * self.days * len(self.shifts)

    @property
    def total_demand(self) -> int:
        return sum(self.demand.values())


# --- bucketing hourly volumes into shifts ----------------------------------


def shift_of_hour(hour: int) -> str:
    """Which shift an hour-of-day falls in. Total over 0..23, no gaps."""
    for shift, (start, end) in SHIFT_HOURS.items():
        if start < end:
            if start <= hour < end:
                return shift
        elif hour >= start or hour < end:  # wraps midnight
            return shift
    raise ValueError(f"hour out of range: {hour}")  # pragma: no cover


def _epoch_ms(value: Any) -> int:
    """Epoch milliseconds from whatever the loader handed back.

    ``models._data`` documents ``hour_ts`` as epoch ms and its synthetic
    fallback produces exactly that, but the real query is
    ``date_trunc('HOUR', ...)``, which Spark returns as a ``datetime``. Accept
    both rather than working on one of the two paths only.
    """
    if isinstance(value, bool):  # pragma: no cover - defensive
        raise TypeError("hour_ts must not be a bool")
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return int(moment.timestamp() * 1000)
    if isinstance(value, date):
        midnight = datetime(value.year, value.month, value.day, tzinfo=UTC)
        return int(midnight.timestamp() * 1000)
    moment = datetime.fromisoformat(str(value))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return int(moment.timestamp() * 1000)


def hourly_volumes(rows: list[dict[str, Any]], *, days: int) -> dict[tuple[int, str], float]:
    """Bucket hourly trip counts into ``days`` x 3 shift volumes.

    Returns ``{}`` if the rows cannot produce a usable curve, which is the
    caller's signal to fall back to a flat requirement.

    Real data is never as tidy as the fallback: quiet hours vanish (the query
    has ``HAVING COUNT(*) > 0``) and the first and last calendar days of a
    ``LIMIT``-ed window are partial. A partial day is worse than no day — it
    looks like a genuine trough and drags the whole calibration down — so days
    are preferred whole, and if fewer whole days survive than the planning
    window needs, the curve is cycled. The model's size is fixed by ``days``,
    never by how much data turned up.
    """
    by_day: dict[int, dict[str, float]] = {}
    hours_seen: dict[int, set[int]] = {}
    for row in rows:
        try:
            ts = _epoch_ms(row["hour_ts"])
            trips = float(row["trips"])
        except (KeyError, TypeError, ValueError):
            continue
        day_ordinal = ts // _MS_PER_DAY
        hour = int((ts // _MS_PER_HOUR) % 24)
        bucket = by_day.setdefault(day_ordinal, dict.fromkeys(SHIFTS, 0.0))
        bucket[shift_of_hour(hour)] += trips
        hours_seen.setdefault(day_ordinal, set()).add(hour)

    covered = [o for o in sorted(by_day) if all(by_day[o][s] > 0 for s in SHIFTS)]
    whole = [o for o in covered if len(hours_seen[o]) >= MIN_HOURS_PER_DAY]
    ordinals = whole or covered or sorted(by_day)
    if not ordinals:
        return {}

    return {
        (d, shift): by_day[ordinals[d % len(ordinals)]][shift]
        for d in range(days)
        for shift in SHIFTS
    }


# --- demand from the curve --------------------------------------------------


def _fit_to_capacity(
    demand: dict[tuple[int, str], int],
    *,
    days: int,
    available_per_day: list[int],
    staff_count: int,
    max_shifts_per_staff: int,
) -> bool:
    """Shrink demand until the workforce that exists can actually cover it.

    Deliberately one-directional: if the real demand curve implies more staff
    than fit, the *requirement* is scaled down. Growing the instance instead
    would mean more staff, more variables, and the bundled restricted licence
    caps us at 2000 of those — that cap is not negotiable, so demand yields.

    Three ceilings, each matching a constraint the MILP actually has:

    * a shift cannot need more people than are available and free that day
      (``one_shift`` means one person covers one shift per day);
    * a day's total cannot exceed the staff available that day;
    * the window's total cannot exceed ``staff_count x max_shifts_per_staff``.

    Returns whether anything was clipped, so the run can say so.
    """
    clipped = False

    for d in range(days):
        keys = [(d, shift) for shift in SHIFTS]
        shift_cap = max(MIN_DEMAND, int(available_per_day[d] * SHIFT_UTILISATION_CEILING))
        for key in keys:
            if demand[key] > shift_cap:
                demand[key] = shift_cap
                clipped = True

        day_cap = max(len(SHIFTS) * MIN_DEMAND, int(available_per_day[d] * DAY_UTILISATION_CEILING))
        while sum(demand[k] for k in keys) > day_cap:
            key = _largest(demand, keys)
            if key is None:
                break
            demand[key] -= 1
            clipped = True

    window_cap = max(
        days * len(SHIFTS) * MIN_DEMAND,
        int(staff_count * max_shifts_per_staff * WINDOW_UTILISATION_CEILING),
    )
    keys = sorted(demand)
    while sum(demand.values()) > window_cap:
        key = _largest(demand, keys)
        if key is None:
            break
        demand[key] -= 1
        clipped = True

    return clipped


def _largest(
    demand: dict[tuple[int, str], int], keys: list[tuple[int, str]]
) -> tuple[int, str] | None:
    """The busiest shift that still has slack. Ties break on (day, shift) so
    the whole procedure is deterministic."""
    candidates = [k for k in keys if demand[k] > MIN_DEMAND]
    if not candidates:
        return None
    return max(candidates, key=lambda k: (demand[k], -k[0], -SHIFTS.index(k[1])))


def _demand_from_curve(
    volumes: dict[tuple[int, str], float],
    *,
    staff_count: int,
    trips_per_staff: float | None,
    target_utilisation: float,
) -> tuple[dict[tuple[int, str], int], float]:
    """Trips in a shift bucket -> people needed on that shift.

    ``trips_per_staff`` is the ratio one member of staff can absorb. Give it a
    number and it is used as given. Leave it ``None`` (the default) and it is
    calibrated off the data so the *average* day lands at
    ``staff_count * target_utilisation`` people — which is what makes this work
    on both data paths: ``samples.nyctaxi.trips`` runs at tens of trips an hour
    while the synthetic fallback runs at hundreds, and any single hard-coded
    ratio flattens one of the two to the ``MIN_DEMAND`` floor and throws the
    demand curve away. The *shape* of the curve is the real signal here; its
    absolute scale is an artefact of how big a sample the table happens to be.
    """
    mean_bucket = sum(volumes.values()) / len(volumes)
    if trips_per_staff is None:
        target_per_bucket = max(1.0, staff_count * target_utilisation / len(SHIFTS))
        trips_per_staff = max(1e-9, mean_bucket / target_per_bucket)

    demand = {
        key: max(MIN_DEMAND, round(value / trips_per_staff)) for key, value in volumes.items()
    }
    return demand, float(trips_per_staff)


# --- the instance -----------------------------------------------------------


def build_instance(
    *,
    staff_count: int = 20,
    days: int = 14,
    seed: int = 20260822,
    max_shifts_per_staff: int = 10,
    demand_per_shift: tuple[int, int, int] = (4, 3, 2),
    use_sample_data: bool = True,
    demand_data: Dataset | None = None,
    trips_per_staff: float | None = None,
    target_utilisation: float = TARGET_UTILISATION,
) -> Instance:
    """A fixed instance for a given seed. Same seed, same problem, always.

    Availability, cost and preference are generated from ``seed``. Demand comes
    from the hourly trip curve in ``models._data`` — real rows on a workspace,
    a deterministic synthetic curve otherwise — unless ``use_sample_data`` is
    ``False``, which gives the flat ``demand_per_shift`` requirement and reads
    nothing at all. Pass ``demand_data`` to supply a curve directly.
    """
    rng = random.Random(seed)
    staff = tuple(f"staff-{i:02d}" for i in range(staff_count))

    # Draw order is fixed: changing it would change every seeded instance.
    # Roughly one person in ten is unavailable on any given day.
    available = {(s, d): rng.random() > 0.1 for s in staff for d in range(days)}
    cost = {(s, shift): 1.0 + rng.random() for s in staff for shift in SHIFTS}
    preference = {(s, shift): rng.choice((0.0, 0.0, 0.3)) for s in staff for shift in SHIFTS}
    available_per_day = [sum(available[(s, d)] for s in staff) for d in range(days)]

    dataset: Dataset | None = demand_data
    if dataset is None and use_sample_data:
        dataset = nyc_taxi_hourly(days=days + CURVE_SLACK_DAYS)

    volumes: dict[tuple[int, str], float] = {}
    curve_provenance = ""
    curve_meta: dict[str, Any] = {}
    if dataset is not None:
        volumes = hourly_volumes(dataset.rows, days=days)
        curve_provenance = dataset.provenance
        curve_meta = dict(dataset.describe())

    if volumes:
        demand, ratio = _demand_from_curve(
            volumes,
            staff_count=staff_count,
            trips_per_staff=trips_per_staff,
            target_utilisation=target_utilisation,
        )
        demand_meta: dict[str, Any] = {
            "demand_derived_from": "hourly_trip_volumes",
            "demand_trips_per_staff": round(ratio, 4),
        }
        provenance = f"coverage demand from {curve_provenance}"
        data_meta = curve_meta
    else:
        demand = {
            (d, shift): demand_per_shift[k]
            for d in range(days)
            for k, shift in enumerate(SHIFTS)
        }
        why = (
            "sample data not requested"
            if not use_sample_data
            else "the hourly curve had no usable whole days"
        )
        demand_meta = {"demand_derived_from": "flat_demand_per_shift"}
        provenance = f"coverage demand is flat {demand_per_shift}: {why}"
        data_meta = {
            "data_source": "synthetic:flat-demand",
            "data_synthetic": True,
            "data_rows": 0,
            "data_fallback_reason": why,
        }

    demand_meta["demand_clipped_to_capacity"] = _fit_to_capacity(
        demand,
        days=days,
        available_per_day=available_per_day,
        staff_count=staff_count,
        max_shifts_per_staff=max_shifts_per_staff,
    )
    demand_meta["demand_total"] = sum(demand.values())

    # The results table has a fixed schema: every provenance key is present on
    # every row, null rather than absent. `Dataset.describe()` guarantees this
    # too; the setdefault covers the hand-built branch above and any loader
    # that stops.
    data_meta.setdefault("data_fallback_reason", None)

    return Instance(
        staff=staff,
        days=days,
        demand=demand,
        available=available,
        cost=cost,
        preference=preference,
        max_shifts_per_staff=max_shifts_per_staff,
        provenance=provenance,
        data_meta=data_meta,
        demand_meta=demand_meta,
    )
