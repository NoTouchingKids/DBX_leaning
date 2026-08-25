"""Deterministic instance generation for the vehicle-routing MILP.

The stops are derived from **real trips** — individual NYC taxi trips out of
Databricks' free ``samples`` catalog, via ``models._data`` — in the three ways
a routing instance can honestly use them:

* **how far out a stop is.** Each stop sits at radius = one real
  ``trip_distance`` from the depot, so the service area has the scale and the
  spread of real journeys rather than of a uniform square. The *angle* is
  generated (a golden-angle spiral, rotated by the seed): the taxi sample has
  no coordinates, and pretending otherwise would be dressing a random number
  up as data.
* **how long a stop takes.** Service time = one real ``duration_min``.
* **what distance costs.** ``cost_per_distance`` is the median observed
  fare per mile — a real price, not a made-up coefficient.

Off a workspace ``models._data`` falls back to a deterministic generator of
the same shape, so this module runs standalone and its tests need no
Databricks. Which of the two happened is recorded on the ``Instance``
(``provenance`` / ``data_meta``) and carried into every result row.

Sizing is the hard constraint here, not the data: the bundled restricted
licence caps the model at 2000 variables (see
``job/models/gurobi_scheduling/LICENCE_EXPIRY.md``), and an edge-formulated
routing model grows quadratically — ``n`` stops cost ``n(n-1)/2 + n``
variables. ``MAX_STOPS`` is where that stops fitting with headroom, and
``build_instance`` refuses rather than building something the licence will
reject at solve time.
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, field
from typing import Any

from .._data import Dataset, nyc_taxi_trips

__all__ = [
    "Stop",
    "Instance",
    "build_instance",
    "MAX_STOPS",
    "variable_count_for",
]

#: n(n-1)/2 edge variables + n depot variables. 55 stops = 1540 variables,
#: which leaves real headroom under the 2000 cap; 62 would already be 1953.
MAX_STOPS = 55

#: Radii are clamped before they become geometry. The real table contains
#: 40-mile airport runs, and one of those placed among 2-mile hops makes the
#: instance a star rather than a routing problem.
MIN_RADIUS = 0.25
MAX_RADIUS = 15.0

#: Service time per stop, minutes. Same reasoning: a real duration column has
#: outliers, and an outlier here silently forces a vehicle of its own.
MIN_SERVICE_MINUTES = 2.0
MAX_SERVICE_MINUTES = 45.0

#: Fallback price per unit distance when the trips have no usable fares.
DEFAULT_COST_PER_DISTANCE = 2.6

#: Vehicle capacity = total service minutes / vehicles, with this much slack.
#: At exactly 1.0 the only feasible solutions are perfect partitions, which is
#: a bin-packing puzzle wearing a routing problem's clothes.
CAPACITY_SLACK = 1.15

#: Golden angle: spreads n points around the depot without any two sharing a
#: bearing, for any n, with no rejection sampling.
GOLDEN_ANGLE = math.pi * (3.0 - math.sqrt(5.0))


def variable_count_for(stop_count: int) -> int:
    """Variables an instance of this size will build. Quadratic — that is the
    whole reason ``MAX_STOPS`` exists."""
    return stop_count * (stop_count - 1) // 2 + stop_count


@dataclass(frozen=True)
class Stop:
    name: str
    x: float
    y: float
    #: How long the vehicle spends here. Charged against the vehicle's
    #: capacity; the travel between stops is not (see ``Instance``).
    service_minutes: float


@dataclass(frozen=True)
class Instance:
    """A capacitated vehicle-routing instance.

    ``capacity_minutes`` is a vehicle's service-minute budget for the shift.
    Travel time is deliberately *not* charged against it: doing so turns this
    into a route-duration-constrained VRP, which needs arc-level time
    variables and would blow the variable cap several times over. What is
    charged is honest — the time spent at the stops the vehicle serves.
    """

    stops: tuple[Stop, ...]
    vehicles: int
    capacity_minutes: float
    cost_per_distance: float
    depot: tuple[float, float] = (0.0, 0.0)
    #: One line for a log message at the ``input`` phase.
    provenance: str = "generated stops (no dataset read)"
    #: ``Dataset.describe()`` fields, carried into every result row.
    data_meta: dict[str, Any] = field(default_factory=dict)
    #: How the trips became geometry, for the record.
    routing_meta: dict[str, Any] = field(default_factory=dict)

    @property
    def stop_count(self) -> int:
        return len(self.stops)

    @property
    def variable_count(self) -> int:
        return variable_count_for(len(self.stops))

    @property
    def total_service_minutes(self) -> float:
        return sum(stop.service_minutes for stop in self.stops)

    def point(self, node: int) -> tuple[float, float]:
        """Node 0 is the depot; 1..n are the stops, in order."""
        if node == 0:
            return self.depot
        stop = self.stops[node - 1]
        return (stop.x, stop.y)

    def distance(self, a: int, b: int) -> float:
        (ax, ay), (bx, by) = self.point(a), self.point(b)
        return math.hypot(ax - bx, ay - by)

    def cost(self, a: int, b: int) -> float:
        return self.distance(a, b) * self.cost_per_distance

    def demand(self, node: int) -> float:
        return 0.0 if node == 0 else self.stops[node - 1].service_minutes


# --- turning trips into geometry -------------------------------------------


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _usable(data: Dataset) -> Dataset:
    """Rows with all three columns present and finite.

    A real ``fare_amount`` can be NULL and ``float(None)`` raises from deep
    inside the geometry, on a workspace only — which is the worst shape a bug
    can have. Drop whole rows so the three columns stay aligned.
    """
    return data.dropna("trip_distance", "fare_amount", "duration_min")


def _cost_per_distance(rows: list[dict[str, Any]]) -> float:
    """Median observed fare per unit distance. Median, not mean: short trips
    have a fixed component that makes their per-mile rate enormous, and a mean
    over those is a number about the flag-fall, not about distance."""
    rates = [
        float(row["fare_amount"]) / float(row["trip_distance"])
        for row in rows
        if float(row["trip_distance"]) > 0.0
    ]
    if not rates:
        return DEFAULT_COST_PER_DISTANCE
    return round(statistics.median(rates), 4)


def _stops_from_trips(
    rows: list[dict[str, Any]], *, stop_count: int, rng: random.Random
) -> tuple[Stop, ...]:
    rotation = rng.random() * 2.0 * math.pi
    stops = []
    for index in range(stop_count):
        row = rows[index]
        radius = _clamp(float(row["trip_distance"]), MIN_RADIUS, MAX_RADIUS)
        service = _clamp(float(row["duration_min"]), MIN_SERVICE_MINUTES, MAX_SERVICE_MINUTES)
        angle = (rotation + index * GOLDEN_ANGLE + rng.uniform(-0.06, 0.06)) % (2.0 * math.pi)
        stops.append(
            Stop(
                name=f"stop-{index:02d}",
                x=round(radius * math.cos(angle), 6),
                y=round(radius * math.sin(angle), 6),
                service_minutes=round(service, 4),
            )
        )
    return tuple(stops)


def _generated_stops(*, stop_count: int, rng: random.Random) -> tuple[Stop, ...]:
    """The offline shape of the same thing: a lognormal radius standing in for
    the trip-distance distribution, so an instance built without any dataset
    is the same *kind* of problem, not a different one."""
    stops = []
    for index in range(stop_count):
        radius = _clamp(rng.lognormvariate(0.6, 0.7), MIN_RADIUS, MAX_RADIUS)
        service = _clamp(3.0 + radius * 3.2, MIN_SERVICE_MINUTES, MAX_SERVICE_MINUTES)
        angle = (index * GOLDEN_ANGLE + rng.uniform(-0.06, 0.06)) % (2.0 * math.pi)
        stops.append(
            Stop(
                name=f"stop-{index:02d}",
                x=round(radius * math.cos(angle), 6),
                y=round(radius * math.sin(angle), 6),
                service_minutes=round(service, 4),
            )
        )
    return tuple(stops)


# --- the instance -----------------------------------------------------------


def build_instance(
    *,
    stop_count: int = 24,
    vehicles: int = 3,
    seed: int = 20260823,
    use_sample_data: bool = True,
    trip_data: Dataset | None = None,
    capacity_slack: float = CAPACITY_SLACK,
    trip_limit: int = 2000,
) -> Instance:
    """A fixed instance for a given seed. Same seed, same problem, always.

    Stops come from real trips (``models._data``) unless ``use_sample_data``
    is ``False``, which reads nothing at all and generates them instead. Pass
    ``trip_data`` to supply the trips directly.

    Raises ``ValueError`` above ``MAX_STOPS`` — the licence cap is not
    negotiable and an edge formulation grows quadratically, so this is the one
    knob that has to refuse rather than clip.
    """
    if stop_count < 2:
        raise ValueError(f"stop_count must be at least 2, got {stop_count}")
    if stop_count > MAX_STOPS:
        raise ValueError(
            f"stop_count={stop_count} would build {variable_count_for(stop_count)} variables; "
            f"the bundled restricted licence caps the model at 2000 "
            f"(see job/models/gurobi_scheduling/LICENCE_EXPIRY.md). Maximum is {MAX_STOPS} stops."
        )
    if not 1 <= vehicles <= stop_count:
        raise ValueError(f"vehicles must be between 1 and stop_count={stop_count}, got {vehicles}")

    rng = random.Random(seed)

    data: Dataset | None = trip_data
    if data is None and use_sample_data:
        data = nyc_taxi_trips(limit=max(trip_limit, stop_count))

    rows: list[dict[str, Any]] = []
    data_meta: dict[str, Any] = {}
    curve_provenance = ""
    if data is not None:
        usable = _usable(data)
        data_meta = dict(usable.describe())
        curve_provenance = usable.provenance
        if len(usable.rows) >= stop_count:
            rows = usable.rows

    if rows:
        stops = _stops_from_trips(rows, stop_count=stop_count, rng=rng)
        cost_per_distance = _cost_per_distance(rows)
        provenance = f"routing stops from {curve_provenance}"
        routing_meta: dict[str, Any] = {"stops_derived_from": "trip_distance_and_duration"}
    else:
        stops = _generated_stops(stop_count=stop_count, rng=rng)
        cost_per_distance = DEFAULT_COST_PER_DISTANCE
        why = (
            "sample data not requested"
            if not use_sample_data
            else f"fewer than {stop_count} usable trips"
        )
        provenance = f"routing stops generated: {why}"
        routing_meta = {"stops_derived_from": "generated_radii"}
        data_meta = {
            "data_source": "synthetic:generated-stops",
            "data_synthetic": True,
            "data_rows": 0,
            "data_fallback_reason": why,
        }

    total_service = sum(stop.service_minutes for stop in stops)
    longest_stop = max(stop.service_minutes for stop in stops)
    # A vehicle must be able to hold at least the single longest stop, or the
    # instance is infeasible for a reason that has nothing to do with routing.
    capacity = max(longest_stop, math.ceil(total_service / vehicles * capacity_slack))

    routing_meta.update(
        {
            "cost_per_distance": cost_per_distance,
            "capacity_minutes": capacity,
            "total_service_minutes": round(total_service, 4),
            "vehicles": vehicles,
            "minimum_vehicles": math.ceil(total_service / capacity - 1e-9),
        }
    )
    # The results table has a fixed schema: every provenance key present on
    # every row, null rather than absent.
    data_meta.setdefault("data_fallback_reason", None)

    return Instance(
        stops=stops,
        vehicles=vehicles,
        capacity_minutes=float(capacity),
        cost_per_distance=cost_per_distance,
        provenance=provenance,
        data_meta=data_meta,
        routing_meta=routing_meta,
    )
