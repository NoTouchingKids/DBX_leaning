"""A tick a second, with a status transition and a cancel check.

Small on purpose, but not trivial on purpose: it exercises every message type
the envelope has except `result`, and it polls cancellation the way a real
model's solver callback would. A heartbeat that only emitted logs would prove
less than it appears to.

It writes no results and reads no data. That is not a simplification to be
fixed later — a model owns its own data lifecycle in v4, and a heartbeat has
none. `result` messages get exercised when the first real model lands.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

__all__ = ["Heartbeat", "build_model"]

#: Ticks per second. A real model samples progress a few times a second at
#: most (see the envelope spec); this sits at the slow end of that on purpose,
#: because the interesting question is whether a tick ARRIVES, not throughput.
DEFAULT_HZ = 1.0
DEFAULT_SECONDS = 60.0


class Heartbeat:
    def __init__(self, seconds: float = DEFAULT_SECONDS, hz: float = DEFAULT_HZ) -> None:
        self.seconds = max(0.0, float(seconds))
        self.hz = max(0.01, float(hz))
        self.ticks = 0
        self._emit: Callable[..., Any] | None = None
        self._should_cancel: Callable[[], bool] = lambda: False

    # The harness calls this and nothing else to wire the model up. The model
    # never learns what is on the other end of `emit`.
    def attach(self, emit: Callable[..., Any], should_cancel: Callable[[], bool]) -> None:
        self._emit = emit
        self._should_cancel = should_cancel

    def run(self) -> str:
        """Tick until the time is up or a cancel arrives.

        Returns the status it thinks the run reached. The harness decides the
        final one — a cancel observed here and a cancel observed there must
        not disagree — but a model saying what it believes is how a
        model-defined status reaches the wire at all.
        """
        emit = self._emit or (lambda *_a, **_k: None)
        interval = 1.0 / self.hz
        total = max(1, int(self.seconds * self.hz))

        emit("log", message=f"heartbeat starting: {total} ticks at {self.hz}Hz", phase="run")

        started = time.monotonic()
        for tick in range(total):
            if self._should_cancel():
                emit(
                    "log",
                    message=f"cancel observed at tick {tick}",
                    level="WARNING",
                    phase="run",
                )
                return "CANCELLED"

            elapsed = time.monotonic() - started
            self.ticks = tick + 1
            emit(
                "progress",
                elapsed_seconds=elapsed,
                percent_complete=100.0 * self.ticks / total,
                primary_metric=float(self.ticks),
                primary_metric_label="ticks",
                payload={"tick": self.ticks, "of": total},
            )

            # Sleep in slices so a cancel is noticed within ~100ms rather than
            # after a whole interval. A real solver does the same thing by
            # polling inside its own callback — this is what `ortools_jobshop`
            # had to learn on v3, where a CP-SAT callback firing only on
            # improvement left a cancel unseen for the whole time limit.
            deadline = time.monotonic() + interval
            while time.monotonic() < deadline:
                if self._should_cancel():
                    break
                time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))

        emit("log", message=f"heartbeat done: {self.ticks} ticks", phase="run")
        return "SUCCEEDED"


def build_model(config: dict[str, Any] | None = None) -> Heartbeat:
    cfg = config or {}
    return Heartbeat(
        seconds=float(cfg.get("seconds", DEFAULT_SECONDS)),
        hz=float(cfg.get("hz", DEFAULT_HZ)),
    )
