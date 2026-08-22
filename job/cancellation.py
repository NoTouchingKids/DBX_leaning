"""Cancellation, as a ``threading.Event``.

Deliberately not an ``asyncio.Event``: the thing that has to observe it is the
model's blocking call, running on a worker thread. The event loop sets it in
response to a cancel command arriving over the WebSocket; the model polls it.

There is no durable/warehouse-polling fallback for cancel on this side. If no
live channel exists at all, the operator escape hatch is
``databricks jobs cancel-run`` — a hard kill, outside this harness's control.
Documented rather than built around.
"""

from __future__ import annotations

import threading

__all__ = ["CancellationToken"]


class CancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._reason: str | None = None
        self._lock = threading.Lock()

    def cancel(self, reason: str | None = None) -> None:
        with self._lock:
            if self._reason is None:
                self._reason = reason or "cancelled"
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def __call__(self) -> bool:
        """So a model can just call ``self.should_cancel()``."""
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason
