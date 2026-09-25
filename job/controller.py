"""The controller thread: everything the harness does that is not the model.

One thread, one FIFO of work, one timer. It exists so that nothing slow ever
runs on the main thread, where the model is:

* **the roll timer** — age telemetry parts out on `tick_s`, which is what makes
  the 30s age bound real (the work the old `roller` thread did);
* **RPC dispatch** — every `cancel`, `replay`, `ping` and the handling of
  `hello`'s answer, handed over by the socket's receiver thread;
* **the status writer** — every `status` message's Lakebase write, queued by
  `emit()` and never called on the model's thread.

FIFO is the property that matters: a status write queued before a cancel runs
before it, and a replay sees every roll that preceded it. The price is that a
slow item delays the ones behind it — which is why each item's owner bounds it
(the Lakebase writer's connect/statement timeouts, a UC close's ~117ms) and why
the harness bounds how long it WAITS for this thread at shutdown.

Blocking read, not polling: the loop sleeps in `queue.get` until either work
arrives or the next tick is due, so an idle run costs one wake-up per tick.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

__all__ = ["Controller"]

_STOP = object()


class Controller:
    """A single worker thread with a timer. Never raises into a caller."""

    def __init__(
        self,
        *,
        tick_s: float,
        on_tick: Callable[[], Any] | None = None,
        name: str = "controller",
    ) -> None:
        self._tick_s = max(tick_s, 0.001)
        self._on_tick = on_tick
        self._name = name
        self._q: queue.Queue[Any] = queue.Queue()
        self._closing = threading.Event()
        self._thread: threading.Thread | None = None

        #: Work items that raised. Each was logged; none reached a caller.
        self.failures = 0

    @property
    def thread(self) -> threading.Thread | None:
        return self._thread

    def on_thread(self) -> bool:
        """Is the caller running on this controller? For assertions and tests."""
        return self._thread is not None and threading.current_thread() is self._thread

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name=self._name, daemon=True)
        self._thread.start()

    def submit(self, fn: Callable[[], Any]) -> bool:
        """Queue `fn` to run on the controller. Never blocks, never raises.

        False once `stop()` has been called — the caller decides what an
        unaccepted item means (the RPC client runs it where it stands, so a
        request is never silently unanswered).
        """
        if self._closing.is_set():
            return False
        self._q.put(fn)
        return True

    def drain(self, timeout: float) -> bool:
        """Wait, at most `timeout` seconds, until everything submitted so far
        has run. True if it all did. The wait is bounded; the work is not —
        an item still running when this returns False keeps running."""
        if self._thread is None or not self._thread.is_alive():
            return False
        done = threading.Event()
        if not self.submit(done.set):
            return False
        return done.wait(timeout)

    def stop(self, timeout: float = 1.0) -> None:
        """Run what is already queued, then end. Joins for at most `timeout`;
        the thread is a daemon, so one stuck in a slow item cannot hold the
        process open."""
        self._closing.set()
        self._q.put(_STOP)
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout)

    # --- the thread --------------------------------------------------------

    def _loop(self) -> None:
        next_tick = time.monotonic() + self._tick_s
        while True:
            wait = max(0.0, next_tick - time.monotonic())
            try:
                item = self._q.get(timeout=wait)
            except queue.Empty:
                item = None
            if item is _STOP:
                return
            if item is not None:
                self._run(item)
            if self._on_tick is not None and time.monotonic() >= next_tick:
                self._run(self._on_tick)
                next_tick = time.monotonic() + self._tick_s

    def _run(self, fn: Callable[[], Any]) -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001 - one bad item must not end the thread
            self.failures += 1
            log.exception("controller work item raised; the controller carries on")
