"""The durable path. Runs always, in parallel with whatever live channel is
up — it is the floor, not a fallback tier.

Nothing here drops anything. A failed write puts its rows back in the buffer
and is retried on the next tick; the failure is remembered so the runner can
refuse to report ``SUCCEEDED`` over a lost result write.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from shared.envelope import Message
from shared.tables import TableSet, table_for, to_row

from .buffer import DurableBuffer
from .delta import BatchWriter

log = logging.getLogger(__name__)

__all__ = ["DurableSink"]


class DurableSink:
    def __init__(
        self,
        writer: BatchWriter,
        tables: TableSet,
        *,
        max_bytes: int = 1_000_000,
        max_age_s: float = 30.0,
        buffer: DurableBuffer | None = None,
    ) -> None:
        self.writer = writer
        self.tables = tables
        self.buffer = buffer or DurableBuffer()
        self.max_bytes = max_bytes
        self.max_age_s = max_age_s

        self.rows_written = 0
        self.write_failures = 0
        self.last_error: str | None = None
        self._flush_lock = asyncio.Lock()

    # --- append (called from any thread, never blocks on I/O) -------------

    def append_message(self, msg: Message) -> None:
        self.buffer.append(self.tables.qualify(table_for(msg)), to_row(msg))

    def append_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        qualified = self.tables.qualify(table)
        for row in rows:
            self.buffer.append(qualified, row)

    # --- flush (event loop side; the write itself goes off-loop) ----------

    async def flush_due(self) -> int:
        return await self._flush(self.buffer.due(max_bytes=self.max_bytes, max_age_s=self.max_age_s))

    async def flush_all(self) -> int:
        async with self._flush_lock:
            pending = self.buffer.take_all()
            written = 0
            for table, rows in pending.items():
                written += await self._write(table, rows)
            return written

    async def _flush(self, tables: list[str]) -> int:
        if not tables:
            return 0
        async with self._flush_lock:
            written = 0
            for table in tables:
                rows = self.buffer.take(table)
                written += await self._write(table, rows)
            return written

    async def _write(self, table: str, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        try:
            # write_batch is blocking; keeping it off the loop is what lets a
            # WebSocket stay alive across a flush.
            count = await asyncio.to_thread(self.writer.write_batch, table, rows)
        except Exception as exc:  # noqa: BLE001 - every failure mode is "retry later"
            self.write_failures += 1
            self.last_error = f"{table}: {exc}"
            self.buffer.restore(table, rows)
            log.warning("durable write to %s failed (%s); %d rows requeued", table, exc, len(rows))
            return 0
        self.rows_written += count
        return count

    @property
    def healthy(self) -> bool:
        """False once anything has been left unwritten.

        Turned back to True only by a later successful flush of everything —
        see ``unflushed``, which the runner checks before it is allowed to
        report success.
        """
        return self.unflushed == 0

    @property
    def unflushed(self) -> int:
        return self.buffer.stats().rows
