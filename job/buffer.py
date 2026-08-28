"""The durable write buffer.

Buffers per destination table, independently: flushing ``run_logs`` must not
block ``run_progress`` or a model's results table.

Flush on whichever comes first — size >= 1 MB, age >= 30s, or end of run. The
**age** bound is the one that caps data loss when the process dies; size
alone is not a durability guarantee, because a slow run may never reach 1 MB.

Thread-safe: rows arrive from the model's worker thread; flushes happen from
the event loop side.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

import msgpack

__all__ = ["DurableBuffer", "BufferStats"]


@dataclass
class _Pending:
    rows: list[dict[str, Any]] = field(default_factory=list)
    nbytes: int = 0
    first_at: float = 0.0


@dataclass(frozen=True)
class BufferStats:
    tables: int
    rows: int
    nbytes: int
    oldest_age_s: float


class DurableBuffer:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self._appended = 0

    def append(self, table: str, row: dict[str, Any]) -> None:
        # msgpack is the buffer's encoding per the spec; packing here also
        # gives an honest byte count for the size threshold, rather than a
        # guess based on row count.
        size = len(msgpack.packb(row, use_bin_type=True, default=str))
        now = time.monotonic()
        with self._lock:
            pending = self._pending.get(table)
            if pending is None:
                pending = self._pending[table] = _Pending(first_at=now)
            if not pending.rows:
                pending.first_at = now
            pending.rows.append(row)
            pending.nbytes += size
            self._appended += 1

    def due(self, *, max_bytes: int, max_age_s: float) -> list[str]:
        now = time.monotonic()
        with self._lock:
            return [
                table
                for table, p in self._pending.items()
                if p.rows and (p.nbytes >= max_bytes or (now - p.first_at) >= max_age_s)
            ]

    def take(self, table: str) -> list[dict[str, Any]]:
        with self._lock:
            pending = self._pending.get(table)
            if pending is None or not pending.rows:
                return []
            rows, pending.rows = pending.rows, []
            pending.nbytes = 0
            pending.first_at = time.monotonic()
            return rows

    def take_all(self) -> dict[str, list[dict[str, Any]]]:
        with self._lock:
            out = {t: p.rows for t, p in self._pending.items() if p.rows}
            for p in self._pending.values():
                p.rows, p.nbytes = [], 0
                p.first_at = time.monotonic()
            return out

    def restore(self, table: str, rows: list[dict[str, Any]]) -> None:
        """Put rows back after a failed write — ahead of anything appended
        since, so ordering within a table survives a retry."""
        if not rows:
            return
        size = sum(len(msgpack.packb(r, use_bin_type=True, default=str)) for r in rows)
        with self._lock:
            pending = self._pending.get(table)
            if pending is None:
                pending = self._pending[table] = _Pending(first_at=time.monotonic())
            pending.rows[:0] = rows
            pending.nbytes += size

    def min_pending_seq(self) -> int | None:
        """Lowest ``seq`` still waiting to be written, across every table.

        This is what makes "how far has Delta caught up" answerable honestly.
        Tables flush independently, so the highest seq *written* is not it: if
        ``run_logs`` has gone out to seq 100 while ``run_progress`` still holds
        seq 50, the warehouse cannot serve 50 and claiming otherwise would
        send a client to fetch a row that is not there. The high-water mark a
        reader can trust is one below the lowest thing still pending.

        Result rows carry no ``seq`` — they are model data, not envelope
        messages — so they are skipped rather than counted as zero.
        """
        with self._lock:
            seqs = [
                row["seq"]
                for pending in self._pending.values()
                for row in pending.rows
                if isinstance(row.get("seq"), int)
            ]
        return min(seqs) if seqs else None

    def stats(self) -> BufferStats:
        now = time.monotonic()
        with self._lock:
            live = [p for p in self._pending.values() if p.rows]
            return BufferStats(
                tables=len(live),
                rows=sum(len(p.rows) for p in live),
                nbytes=sum(p.nbytes for p in live),
                oldest_age_s=max((now - p.first_at for p in live), default=0.0),
            )

    @property
    def appended(self) -> int:
        with self._lock:
            return self._appended
