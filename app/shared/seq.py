"""The per-run sequence counter.

One monotonic counter per run, shared across *all* message types, assigned by
the job. It is the single thing that lets a client dedupe a live record
against a backfilled one with one cursor.

Gap-free by construction is a property of *how this is used*, not of this
class: every message consumes exactly one value, including messages that get
filtered out of the live send (``client_visible=False``) or dropped under
pressure. Nothing is renumbered around a gap. A gap a client observes
therefore always means "these records exist and haven't arrived here yet" —
which, on the live path, is answered by backfilling from Delta rather than by
waiting (logs are allowed to drop live; they are never dropped durably).
"""

from __future__ import annotations

import threading

__all__ = ["SeqCounter"]


class SeqCounter:
    """Thread-safe. The model's callback fires on a worker thread; the
    harness's own status/result messages are stamped on the event loop. Both
    draw from here, so the lock is not optional.
    """

    __slots__ = ("_lock", "_next")

    def __init__(self, start: int = 0) -> None:
        if start < 0:
            raise ValueError("seq cannot start below zero")
        self._lock = threading.Lock()
        self._next = start

    def next(self) -> int:
        with self._lock:
            value = self._next
            self._next += 1
            return value

    @property
    def issued(self) -> int:
        """How many values have been handed out — i.e. the next seq to come."""
        with self._lock:
            return self._next
