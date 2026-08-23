from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from shared.envelope import RunStatus

__all__ = ["Driver", "DriverResult"]


@dataclass(frozen=True)
class DriverResult:
    """What the driver knows about how the run ended.

    ``status`` is the driver's *opinion* — the runner still overrides it with
    CANCELLED if the token was set, and refuses SUCCEEDED if a durable write
    was lost.
    """

    status: RunStatus = RunStatus.SUCCEEDED
    detail: str | None = None


class Driver(Protocol):
    name: str

    def run(self) -> DriverResult:
        """Blocking. Called in a thread executor, never on the event loop."""
