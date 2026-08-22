"""For models that run themselves: ``run()``/``fit()``/``sample()``/...

The overwhelmingly common case. The harness calls the method and gets out of
the way; the model polls ``should_cancel()`` at whatever granularity suits it.
"""

from __future__ import annotations

from typing import Any, Callable

from shared.envelope import RunStatus

from ..loader import ModelHandle
from .base import DriverResult

__all__ = ["SelfDrivingDriver"]


class SelfDrivingDriver:
    name = "self-driving"

    def __init__(
        self,
        handle: ModelHandle,
        emit: Callable[..., Any],
        should_cancel: Callable[[], bool],
    ) -> None:
        self.handle = handle
        self.emit = emit
        self.should_cancel = should_cancel

    def run(self) -> DriverResult:
        assert self.handle.run is not None
        outcome = self.handle.run()

        # A model may return a status (e.g. "INFEASIBLE") to say something the
        # harness could not infer. Anything else it returns is ignored.
        if isinstance(outcome, str):
            try:
                return DriverResult(status=RunStatus(outcome))
            except ValueError:
                return DriverResult(status=RunStatus.SUCCEEDED, detail=outcome)
        if isinstance(outcome, RunStatus):
            return DriverResult(status=outcome)
        return DriverResult()
