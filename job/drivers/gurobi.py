"""The Gurobi driver: the harness owns ``optimize()``.

Gurobi allows exactly one callback slot per model. The harness needs it for
log capture, progress sampling and cancellation, and a model may need it for
lazy constraints or cuts — so the harness *composes* the two rather than
either side taking it exclusively. This is why a Gurobi model must not call
``optimize()`` itself: doing so bypasses cancellation and progress entirely.

``gurobipy`` is imported lazily and can be injected, so this module (and its
tests) work in an environment with no Gurobi installed.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from shared.envelope import RunStatus

from ..loader import ModelHandle
from .base import DriverResult

log = logging.getLogger(__name__)

__all__ = ["GurobiDriver", "GUROBI_SENTINEL"]

#: Gurobi reports ±1e100 for the incumbent/bound before the first feasible
#: solution. It is *finite*, so no NaN/inf guard catches it — storing it raw
#: poisons a chart's axis. Anything at or beyond this is "not known yet".
GUROBI_SENTINEL = 1e100


class GurobiDriver:
    name = "gurobi"

    def __init__(
        self,
        handle: ModelHandle,
        emit: Callable[..., Any],
        should_cancel: Callable[[], bool],
        *,
        progress_every_s: float = 2.0,
        grb: Any = None,
        log_to_client: bool = True,
    ) -> None:
        self.handle = handle
        self.emit = emit
        self.should_cancel = should_cancel
        self.progress_every_s = progress_every_s
        self.log_to_client = log_to_client
        self._grb = grb
        self._last_progress = 0.0
        self._log_tail = ""
        self._terminated = False

    @property
    def grb(self) -> Any:
        if self._grb is None:
            import gurobipy

            self._grb = gurobipy.GRB
        return self._grb

    def run(self) -> DriverResult:
        model = self.handle.gurobi_model
        GRB = self.grb

        # OutputFlag on so MESSAGE callbacks fire; LogToConsole off so lines
        # are not also printed by Gurobi itself and captured twice.
        try:
            model.setParam("OutputFlag", 1)
            model.setParam("LogToConsole", 0)
        except Exception:  # noqa: BLE001 - a stub/mock model in tests
            log.debug("could not set Gurobi output params", exc_info=True)

        model.optimize(self._callback)
        self._flush_log_tail()

        status = getattr(model, "Status", None)
        return DriverResult(status=self._map_status(status, GRB), detail=self._detail(status, GRB))

    # --- the composed callback -------------------------------------------

    def _callback(self, model: Any, where: int) -> None:
        GRB = self.grb
        try:
            self._observe(model, where, GRB)
        except Exception:  # noqa: BLE001 - never let observation kill a solve
            log.debug("gurobi observer raised", exc_info=True)

        own = self.handle.model_callback
        if own is not None:
            try:
                own(model, where)
            except Exception:  # noqa: BLE001
                log.exception("model's own Gurobi callback raised")

    def _observe(self, model: Any, where: int, GRB: Any) -> None:
        if where == GRB.Callback.MESSAGE:
            self._capture_log(model.cbGet(GRB.Callback.MSG_STRING))
            return

        # Cancellation: POLLING fires even when nothing else is happening, MIP
        # fires constantly during branch-and-bound. Either is a fine place to
        # notice. terminate() makes optimize() return INTERRUPTED rather than
        # raising — a user-requested stop is a clean outcome, not an error.
        if where in (GRB.Callback.POLLING, GRB.Callback.MIP, GRB.Callback.SIMPLEX):
            if not self._terminated and self.should_cancel():
                self._terminated = True
                model.terminate()
                return

        if where == GRB.Callback.MIP:
            self._sample_progress(model, GRB)

    def _sample_progress(self, model: Any, GRB: Any) -> None:
        now = time.monotonic()
        if now - self._last_progress < self.progress_every_s:
            return  # MIP fires far too often to emit on every invocation
        self._last_progress = now

        runtime = float(model.cbGet(GRB.Callback.RUNTIME))
        incumbent = _real_or_none(model.cbGet(GRB.Callback.MIP_OBJBST))
        bound = _real_or_none(model.cbGet(GRB.Callback.MIP_OBJBND))
        gap = None
        if incumbent is not None and bound is not None and abs(incumbent) > 1e-10:
            gap = abs(incumbent - bound) / abs(incumbent)

        self.emit(
            "progress",
            elapsed_seconds=runtime,
            # MIP progress is genuinely not a percentage — null, not a guess.
            percent_complete=None,
            primary_metric=gap,
            primary_metric_label="mip_gap",
            payload={
                "best_bound": bound,
                "incumbent": incumbent,
                "nodes_explored": float(model.cbGet(GRB.Callback.MIP_NODCNT)),
                "nodes_remaining": float(model.cbGet(GRB.Callback.MIP_NODLFT)),
                "solution_count": int(model.cbGet(GRB.Callback.MIP_SOLCNT)),
            },
        )

    def _capture_log(self, chunk: str | None) -> None:
        # MESSAGE fires on arbitrary text chunks, not line boundaries. Emitting
        # per chunk produces malformed half-lines, so buffer and split.
        if not chunk:
            return
        self._log_tail += chunk
        *lines, self._log_tail = self._log_tail.split("\n")
        for line in lines:
            if line.strip():
                self.emit(
                    "log",
                    message=line.rstrip(),
                    source="gurobi",
                    phase="solve",
                    client_visible=self.log_to_client,
                )

    def _flush_log_tail(self) -> None:
        if self._log_tail.strip():
            self.emit(
                "log",
                message=self._log_tail.rstrip(),
                source="gurobi",
                phase="solve",
                client_visible=self.log_to_client,
            )
        self._log_tail = ""

    # --- status mapping ---------------------------------------------------

    def _map_status(self, status: Any, GRB: Any) -> RunStatus:
        if status == GRB.INTERRUPTED:
            # Whatever incumbent exists is still a result; the runner turns
            # this into CANCELLED when the token was set.
            return RunStatus.CANCELLED
        if status in (GRB.INFEASIBLE, GRB.INF_OR_UNBD, GRB.UNBOUNDED):
            return RunStatus.INFEASIBLE
        if status in (GRB.OPTIMAL, GRB.SUBOPTIMAL, GRB.TIME_LIMIT, GRB.NODE_LIMIT,
                      GRB.SOLUTION_LIMIT, GRB.ITERATION_LIMIT, GRB.USER_OBJ_LIMIT):
            return RunStatus.SUCCEEDED
        return RunStatus.FAILED

    def _detail(self, status: Any, GRB: Any) -> str | None:
        names = {
            GRB.OPTIMAL: "optimal",
            GRB.SUBOPTIMAL: "suboptimal but feasible",
            GRB.TIME_LIMIT: "time limit reached",
            GRB.INTERRUPTED: "interrupted",
            GRB.INFEASIBLE: "infeasible",
            GRB.UNBOUNDED: "unbounded",
            GRB.INF_OR_UNBD: "infeasible or unbounded",
        }
        return names.get(status, f"gurobi status {status}")


def _real_or_none(value: Any) -> float | None:
    """The ±1e100 pre-incumbent sentinel becomes null, never a raw number."""
    if value is None:
        return None
    value = float(value)
    return None if abs(value) >= GUROBI_SENTINEL else value
