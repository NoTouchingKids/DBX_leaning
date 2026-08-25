"""Capacitated vehicle routing with lazy subtour-elimination constraints.

This is the model that exists because of its callback. A two-index edge
formulation of a routing problem is compact — ``n(n-1)/2 + n`` variables, ``n
+ 1`` constraints — but it is *wrong* on its own: nothing in it says a set of
selected edges has to form routes that pass through the depot. Written out in
full, the constraints that say so number 2^n, which is why nobody writes them
out in full. They are separated instead: solve, look at the integer solution,
find the components that are subtours or that overload a vehicle, add exactly
those constraints, continue. That is what ``gurobi_callback`` does, on
``MIPSOL``, with ``cbLazy``.

Gurobi allows exactly one callback slot per model, and the harness needs it
too — for log capture, progress sampling and cancellation. So the harness
*composes*: it installs its own callback and calls ours from inside it (see
``job/drivers/gurobi.py``). Two consequences for this file:

* it never calls ``optimize()``. The harness owns the solve; calling it here
  would take the callback slot and silently lose cancellation and progress.
* the callback is exposed under the conventional name ``gurobi_callback``
  (``job/loader.py``'s ``CONVENTIONS``), because that is what the composition
  looks for. A privately named method would simply never be called, and the
  model would return solutions full of subtours.

The cut being separated is the rounded-capacity inequality

    sum of edges inside S  <=  |S| - ceil(demand(S) / capacity)

which subsumes plain subtour elimination: a disconnected component has
``k(S) >= 1`` and a component needing two vehicles has ``k(S) = 2``, so one
family of cuts enforces both connectivity to the depot and vehicle capacity.

Stops, service times and the price of distance come from real trips
(``models._data``) — see ``instance.py``. Provenance is logged at the
``input`` phase and carried on every result row.

Size is bounded by the bundled restricted licence: 2000 variables / 2000
constraints, no quadratic terms. See
``job/models/gurobi_scheduling/LICENCE_EXPIRY.md`` for the pin and its expiry.
Lazy constraints do not count against the constraint cap — Gurobi holds them
in the lazy pool and ``NumConstrs`` does not grow — which is a second, quieter
reason a routing model of this size is possible here at all.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterable
from typing import Any

from .instance import Instance, build_instance

__all__ = ["RoutingModel", "build_model"]

#: A run that cannot prove optimality still returns its incumbent — that is
#: the platform's contract. Better to hand back a good route in two minutes
#: than to hold a job slot (there are five, account-wide) for an hour closing
#: the last 0.5% of the gap.
DEFAULT_TIME_LIMIT_S = 120.0

#: Cuts are found in bursts of hundreds; a log line per cut would be noise and
#: would out-pace the live channel. One line per burst, at most this often.
CUT_LOG_INTERVAL_S = 2.0


class RoutingModel:
    #: Discovered by the harness (see job/models/README.md).
    results_table = "results_gurobi_routing"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = dict(config or {})
        self.instance: Instance = cfg.pop("instance", None) or build_instance(
            stop_count=int(cfg.get("stop_count", 24)),
            vehicles=int(cfg.get("vehicles", 3)),
            seed=int(cfg.get("seed", 20260823)),
            # Stops come from the sample catalog by default. Turn it off for a
            # run that must not read anything at all.
            use_sample_data=bool(cfg.get("use_sample_data", True)),
        )
        self.time_limit_s = cfg.get("time_limit_s", DEFAULT_TIME_LIMIT_S)
        self.mip_gap = cfg.get("mip_gap")

        #: Set by the harness before build/run. Never imported from the platform.
        self.emit: Callable[..., Any] | None = None
        self.should_cancel: Callable[[], bool] | None = None

        self.grb_model: Any = None  # the conventional name the harness looks for
        #: (i, j) with i < j, over stop indices 1..n. Node 0 is the depot.
        self._edge: dict[tuple[int, int], Any] = {}
        #: stop -> how many of its two route-ends attach to the depot (0, 1 or
        #: 2). Two means a vehicle serves that stop alone and comes back.
        self._depot_edge: dict[int, Any] = {}

        #: Observable so a test can prove the model's own callback ran, and so
        #: a run can report what separation actually cost.
        self.cuts_added = 0
        self.separation_calls = 0
        self._last_cut_log = 0.0

    # --- build ------------------------------------------------------------

    def build(self) -> None:
        import gurobipy as gp
        from gurobipy import GRB

        inst = self.instance
        # Where the stops came from, before anything else — a run on real
        # trips and a run that fell back must not look the same.
        self._log(inst.provenance, phase="input")
        self._log(
            f"routing {inst.stop_count} stops with {inst.vehicles} vehicles, "
            f"{inst.capacity_minutes:.0f} service-minutes each "
            f"({inst.total_service_minutes:.0f} required), at "
            f"{inst.cost_per_distance} per unit distance",
            phase="input",
        )

        n = inst.stop_count
        model = gp.Model("vehicle_routing")
        # The harness sets these too, but a standalone run should not spray
        # Gurobi's banner over a caller's stdout either.
        model.setParam("OutputFlag", 1)
        model.setParam("LogToConsole", 0)
        # Without this, cbLazy is ignored and the solver returns subtours.
        model.setParam("LazyConstraints", 1)
        if self.time_limit_s:
            model.setParam("TimeLimit", float(self.time_limit_s))
        if self.mip_gap:
            model.setParam("MIPGap", float(self.mip_gap))

        # Undirected edges between stops: x_ij = 1 if a vehicle travels
        # between them. Symmetric, so only i < j exists — half the variables
        # of an arc formulation, and the cap makes that difference matter.
        edge = {
            (i, j): model.addVar(vtype=GRB.BINARY, obj=inst.cost(i, j), name=f"e[{i},{j}]")
            for i in range(1, n + 1)
            for j in range(i + 1, n + 1)
        }
        # Depot edges are integer, not binary: 2 means one vehicle serves this
        # stop alone, out and back. A binary variable here would forbid
        # single-stop routes, which are sometimes optimal.
        depot_edge = {
            j: model.addVar(
                vtype=GRB.INTEGER, lb=0, ub=2, obj=2.0 * inst.cost(0, j), name=f"d[{j}]"
            )
            for j in range(1, n + 1)
        }

        # Degree: every stop is entered once and left once.
        for i in range(1, n + 1):
            model.addConstr(
                gp.quicksum(edge[_key(i, j)] for j in range(1, n + 1) if j != i) + depot_edge[i]
                == 2,
                name=f"degree[{i}]",
            )
        # Every vehicle leaves the depot and comes back.
        model.addConstr(gp.quicksum(depot_edge.values()) == 2 * inst.vehicles, name="depot_degree")

        model.ModelSense = GRB.MINIMIZE
        model.update()

        self.grb_model = model
        self._edge = edge
        self._depot_edge = depot_edge
        self.cuts_added = 0
        self.separation_calls = 0
        self._last_cut_log = 0.0
        self._log(
            f"built: {model.NumVars} vars, {model.NumConstrs} constraints "
            f"(restricted licence cap is 2000/2000); connectivity and capacity "
            f"are separated lazily, not enumerated",
            phase="build",
        )

    # --- the model's own Gurobi callback ----------------------------------

    def gurobi_callback(self, model: Any, where: int) -> None:
        """Separate rounded-capacity cuts at every integer solution.

        Exposed under the name in ``job/loader.py``'s ``CONVENTIONS`` so the
        harness composes this with its own observers instead of one of us
        replacing the other. Gurobi only accepts a candidate as an incumbent
        if this returns without adding a cut for it, so every incumbent — an
        interrupted run's included — is a genuine set of routes.
        """
        import gurobipy as gp
        from gurobipy import GRB

        if where != GRB.Callback.MIPSOL:
            return

        values = model.cbGetSolution(self._edge)
        self.separation_calls += 1
        added = 0
        for component in self._components(values):
            cut = self._violated_cut(component, values)
            if cut is None:
                continue
            stops, needed = cut
            model.cbLazy(
                gp.quicksum(self._edge[pair] for pair in _pairs(stops)) <= len(stops) - needed
            )
            added += 1

        if added:
            self.cuts_added += added
            self._log_cuts()

    def _components(self, values: dict[tuple[int, int], float]) -> Iterable[list[int]]:
        """Connected components of the selected stop-to-stop edges.

        The depot is excluded on purpose: with it in, everything is one
        component and there is nothing to separate. Without it, each component
        is one vehicle's chain of stops — or a subtour floating free of the
        depot entirely.
        """
        n = self.instance.stop_count
        adjacency: dict[int, list[int]] = {i: [] for i in range(1, n + 1)}
        for (i, j), value in values.items():
            if value > 0.5:
                adjacency[i].append(j)
                adjacency[j].append(i)

        seen: set[int] = set()
        for start in range(1, n + 1):
            if start in seen:
                continue
            component = []
            stack = [start]
            seen.add(start)
            while stack:
                node = stack.pop()
                component.append(node)
                for neighbour in adjacency[node]:
                    if neighbour not in seen:
                        seen.add(neighbour)
                        stack.append(neighbour)
            yield sorted(component)

    def _violated_cut(
        self, stops: list[int], values: dict[tuple[int, int], float]
    ) -> tuple[list[int], int] | None:
        """``(S, k(S))`` if ``sum_{e in S} x_e <= |S| - k(S)`` is violated.

        ``k(S)`` is how many vehicles the component's service time needs. A
        legal route through ``S`` is a path — ``|S| - 1`` internal edges — so
        ``k(S) = 1`` makes this exactly subtour elimination, and ``k(S) >= 2``
        makes it a capacity constraint. One family, both jobs.
        """
        inst = self.instance
        load = sum(inst.demand(i) for i in stops)
        needed = max(1, math.ceil(load / inst.capacity_minutes - 1e-9))
        internal = sum(1 for pair in _pairs(stops) if values[pair] > 0.5)
        if internal <= len(stops) - needed:
            return None
        return stops, needed

    def _log_cuts(self) -> None:
        now = time.monotonic()
        if now - self._last_cut_log < CUT_LOG_INTERVAL_S:
            return
        self._last_cut_log = now
        self._log(
            f"separated {self.cuts_added} lazy connectivity/capacity cuts over "
            f"{self.separation_calls} integer solutions",
            phase="solve",
            level="DEBUG",
        )

    # --- results ----------------------------------------------------------

    def results(self) -> list[dict[str, Any]]:
        """Whatever solution exists — including a suboptimal incumbent after a
        cancellation. Never gated on OPTIMAL."""
        model = self.grb_model
        if model is None or getattr(model, "SolCount", 0) < 1:
            return []

        inst = self.instance
        # Provenance travels with every row: the results table is what someone
        # reads six months later, and "were these real trips?" is not a
        # question they should have to answer from the log stream.
        provenance = {
            "data_source": inst.data_meta.get("data_source"),
            "data_synthetic": inst.data_meta.get("data_synthetic"),
            "data_rows": inst.data_meta.get("data_rows"),
            "data_fallback_reason": inst.data_meta.get("data_fallback_reason"),
        }

        rows: list[dict[str, Any]] = []
        for route_index, route in enumerate(self.routes()):
            load = sum(inst.demand(node) for node in route)
            legs = [0, *route, 0]
            distance = sum(inst.distance(a, b) for a, b in zip(legs[:-1], legs[1:], strict=True))
            previous = 0
            for position, node in enumerate(route, start=1):
                stop = inst.stops[node - 1]
                came_from = "depot" if previous == 0 else inst.stops[previous - 1].name
                rows.append(
                    {
                        "route": route_index,
                        "visit_order": position,
                        "stop": stop.name,
                        "previous_stop": came_from,
                        "x": stop.x,
                        "y": stop.y,
                        "service_minutes": stop.service_minutes,
                        "leg_distance": round(inst.distance(previous, node), 6),
                        "leg_cost": round(inst.cost(previous, node), 6),
                        "distance_to_depot": round(inst.distance(node, 0), 6),
                        "route_stops": len(route),
                        "route_load_minutes": round(load, 4),
                        "route_distance": round(distance, 6),
                        "vehicle_capacity_minutes": inst.capacity_minutes,
                        **provenance,
                    }
                )
                previous = node
        return rows

    def routes(self) -> list[list[int]]:
        """The incumbent's routes as lists of stop indices, depot implied at
        both ends. Empty when nothing has been solved.

        Walks the selected edges rather than trusting the solution's shape: a
        malformed solution produces short routes and a warning, never an
        exception in the results path.
        """
        if self.grb_model is None or getattr(self.grb_model, "SolCount", 0) < 1:
            return []

        n = self.instance.stop_count
        adjacency: dict[int, list[int]] = {i: [] for i in range(1, n + 1)}
        for (i, j), var in self._edge.items():
            if var.X > 0.5:
                adjacency[i].append(j)
                adjacency[j].append(i)
        ends = {j: int(round(var.X)) for j, var in self._depot_edge.items() if var.X > 0.5}

        routes: list[list[int]] = []
        used: set[tuple[int, int]] = set()
        while ends:
            start = min(ends)
            _consume(ends, start)
            route = [start]
            node = start
            while True:
                step = next((w for w in sorted(adjacency[node]) if _key(node, w) not in used), None)
                if step is None:
                    break
                used.add(_key(node, step))
                route.append(step)
                node = step
            _consume(ends, node)  # the far end comes back to the depot
            routes.append(route)

        visited = {node for route in routes for node in route}
        stranded = sorted(set(range(1, n + 1)) - visited)
        if stranded:
            # Should be unreachable: the lazy cuts are what make it so. If it
            # happens, report every stop rather than silently losing rows.
            self._log(
                f"{len(stranded)} stops were not reachable from the depot in the "
                f"incumbent; reporting them as single-stop routes",
                phase="results",
                level="WARNING",
            )
            routes.extend([stop] for stop in stranded)
        return routes

    # --- helpers ----------------------------------------------------------

    def _log(self, message: str, *, phase: str = "input", level: str = "INFO") -> None:
        if self.emit is not None:
            self.emit("log", message=message, level=level, source="model", phase=phase)


def _key(i: int, j: int) -> tuple[int, int]:
    return (i, j) if i < j else (j, i)


def _pairs(stops: list[int]) -> Iterable[tuple[int, int]]:
    for index, i in enumerate(stops):
        for j in stops[index + 1 :]:
            yield _key(i, j)


def _consume(ends: dict[int, int], stop: int) -> None:
    """Use up one of a stop's depot connections."""
    if stop not in ends:
        return
    ends[stop] -= 1
    if ends[stop] <= 0:
        del ends[stop]


def build_model(config: dict[str, Any] | None = None) -> RoutingModel:
    return RoutingModel(config)
