"""Staff shift-scheduling MILP.

The original driving use case, and the first model to reach an end-to-end
vertical slice. It exists to prove the platform's transport and envelope work
for a real MILP with genuine branch-and-bound behaviour to stream — it is not
a production optimisation deployment.

Coverage requirements come from a real hourly demand curve (Databricks'
``samples`` catalog, via ``models._data``), not a random number generator —
see ``instance.py``. The provenance of that curve is logged at the ``input``
phase and carried on every result row, so a run on real data and a run that
fell back to the deterministic synthetic curve stay distinguishable.

Note what this class does *not* do: it never calls ``optimize()``. The harness
owns the solve, so it can attach its own progress/log/cancellation observers to
the single callback slot Gurobi allows, composed with ours. Calling
``optimize()`` here would bypass cancellation and progress entirely.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .instance import SHIFTS, Instance, build_instance

__all__ = ["SchedulingModel", "build_model"]


class SchedulingModel:
    #: Discovered by the harness (see job/models/README.md).
    results_table = "results_gurobi_scheduling"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = dict(config or {})
        trips_per_staff = cfg.get("trips_per_staff")
        self.instance: Instance = cfg.pop("instance", None) or build_instance(
            staff_count=int(cfg.get("staff_count", 20)),
            days=int(cfg.get("days", 14)),
            seed=int(cfg.get("seed", 20260822)),
            max_shifts_per_staff=int(cfg.get("max_shifts_per_staff", 10)),
            # Coverage comes from the sample-catalog demand curve by default.
            # Turn it off for a run that must not read anything at all.
            use_sample_data=bool(cfg.get("use_sample_data", True)),
            trips_per_staff=None if trips_per_staff is None else float(trips_per_staff),
        )
        self.time_limit_s = cfg.get("time_limit_s")
        self.mip_gap = cfg.get("mip_gap")

        #: Set by the harness before build/run. Never imported from the platform.
        self.emit: Callable[..., Any] | None = None
        self.should_cancel: Callable[[], bool] | None = None

        self.grb_model: Any = None  # the conventional name the harness looks for
        self._x: dict[tuple[str, int, str], Any] = {}

    # --- build ------------------------------------------------------------

    def build(self) -> None:
        import gurobipy as gp
        from gurobipy import GRB

        inst = self.instance
        # Where the coverage requirement came from, before anything else — a
        # run on real data and a run that fell back must not look the same.
        self._log(inst.provenance, phase="input")
        clipped = inst.demand_meta.get("demand_clipped_to_capacity")
        self._log(
            f"coverage: {inst.total_demand} staff-shifts over {inst.days} days, from "
            f"{inst.demand_meta.get('demand_derived_from')}"
            f"{', clipped to workforce capacity' if clipped else ''}",
            phase="input",
            level="WARNING" if clipped else "INFO",
        )
        self._log(f"building: {len(inst.staff)} staff x {inst.days} days x {len(SHIFTS)} shifts")

        model = gp.Model("staff_scheduling")
        # The harness sets output params too, but a standalone run should not
        # spray Gurobi's banner over a caller's stdout either.
        model.setParam("OutputFlag", 1)
        model.setParam("LogToConsole", 0)
        if self.time_limit_s:
            model.setParam("TimeLimit", float(self.time_limit_s))
        if self.mip_gap:
            model.setParam("MIPGap", float(self.mip_gap))

        x = {}
        for s in inst.staff:
            for d in range(inst.days):
                for shift in SHIFTS:
                    var = model.addVar(vtype=GRB.BINARY, name=f"x[{s},{d},{shift}]")
                    if not inst.available[(s, d)]:
                        # Availability is a bound, not a constraint — it costs
                        # nothing against the 2000-constraint cap.
                        var.UB = 0
                    x[(s, d, shift)] = var

        # Coverage: every shift needs its people.
        for d in range(inst.days):
            for shift in SHIFTS:
                model.addConstr(
                    gp.quicksum(x[(s, d, shift)] for s in inst.staff) >= inst.demand[(d, shift)],
                    name=f"cover[{d},{shift}]",
                )

        # At most one shift per person per day.
        for s in inst.staff:
            for d in range(inst.days):
                model.addConstr(
                    gp.quicksum(x[(s, d, shift)] for shift in SHIFTS) <= 1,
                    name=f"one_shift[{s},{d}]",
                )

        # A cap on total shifts per person over the window.
        for s in inst.staff:
            model.addConstr(
                gp.quicksum(x[(s, d, shift)] for d in range(inst.days) for shift in SHIFTS)
                <= inst.max_shifts_per_staff,
                name=f"max_shifts[{s}]",
            )

        # Rest: a night shift cannot be followed by the next morning.
        for s in inst.staff:
            for d in range(inst.days - 1):
                model.addConstr(
                    x[(s, d, "night")] + x[(s, d + 1, "morning")] <= 1, name=f"rest[{s},{d}]"
                )

        model.setObjective(
            gp.quicksum(
                (inst.cost[(s, shift)] - inst.preference[(s, shift)]) * x[(s, d, shift)]
                for s in inst.staff
                for d in range(inst.days)
                for shift in SHIFTS
            ),
            GRB.MINIMIZE,
        )
        model.update()

        self.grb_model = model
        self._x = x
        self._log(
            f"built: {model.NumVars} vars, {model.NumConstrs} constraints "
            f"(restricted licence cap is 2000/2000)",
            phase="build",
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
        # reads six months later, and "was this real demand?" is not a question
        # they should have to answer from the log stream.
        provenance = {
            "data_source": inst.data_meta.get("data_source"),
            "data_synthetic": inst.data_meta.get("data_synthetic"),
            "data_rows": inst.data_meta.get("data_rows"),
            "data_fallback_reason": inst.data_meta.get("data_fallback_reason"),
        }
        rows = []
        for (s, d, shift), var in self._x.items():
            if var.X > 0.5:
                rows.append(
                    {
                        "staff": s,
                        "day": d,
                        "shift": shift,
                        "cost": round(inst.cost[(s, shift)], 4),
                        "preferred": inst.preference[(s, shift)] > 0,
                        "demand": inst.demand[(d, shift)],
                        **provenance,
                    }
                )
        rows.sort(key=lambda r: (r["day"], r["shift"], r["staff"]))
        return rows

    # --- helpers ----------------------------------------------------------

    def _log(self, message: str, *, phase: str = "input", level: str = "INFO") -> None:
        if self.emit is not None:
            self.emit("log", message=message, level=level, source="model", phase=phase)


def build_model(config: dict[str, Any] | None = None) -> SchedulingModel:
    return SchedulingModel(config)
