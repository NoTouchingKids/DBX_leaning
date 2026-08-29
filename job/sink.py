"""The durable path. Runs always, in parallel with whatever live channel is
up — it is the floor, not a fallback tier.

Nothing here drops anything. A failed write puts its rows back in the buffer
and is retried on the next tick; the failure is remembered so the runner can
refuse to report ``SUCCEEDED`` over a lost result write.

**Nothing in this module touches asyncio, and that is the point.** It was
async once: ``flush_due``/``flush_all`` were coroutines holding an
``asyncio.Lock``, and ``_write`` did
``await asyncio.to_thread(writer.write_batch, ...)`` — an async wrapper whose
whole job was handing a blocking call straight back to a thread. Both ends of
that path were already synchronous. Rows arrive from the model's worker
thread into a ``threading.Lock``-guarded buffer, and the writer is blocking
Spark; the event loop sat in the middle of something that never needed it.

Taking it out buys the property the "floor, not a fallback tier" language
actually demands: **the durable path no longer depends on the event loop
being healthy.** A wedged loop — a long synchronous callback, a socket that
will not drain, a task that never yields — costs the live commentary and
leaves Delta ticking. A floor that stalls with the loop is not one.

``DurableFlusher`` is what replaced the asyncio task: one daemon thread doing
the periodic flush. The loop keeps exactly one hop into any of this, at
teardown (``JobHarness._finalise``), so a Spark write taking seconds cannot
stall the WebSocket drain that follows it.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from .buffer import DurableBuffer
from .delta import BatchWriter
from .shared.envelope import Message
from .shared.tables import TableSet, table_for, to_row
from .stream import StreamCursor

log = logging.getLogger(__name__)

__all__ = ["DurableSink", "DurableFlusher"]

#: Floor on the flush interval. ``DBX_FLUSH_TICK_S=0`` is one typo away in a
#: deploy, and a thread whose wait is zero does not tick — it spins a core for
#: the length of the run, on billed compute, saying nothing about why.
MIN_TICK_S = 0.001

#: How long ``DurableFlusher.stop()`` waits for a tick already in flight.
#: Bounded, because a wedged Spark write must not hold a finished run open;
#: safe to bound, because the thread is a daemon and cannot keep the process
#: alive past it.
STOP_TIMEOUT_S = 30.0


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
        # Serialises the take-then-write pair, so the flusher thread and the
        # runner's final flush cannot both take rows for the same table and
        # write them out of order.
        self._flush_lock = threading.Lock()

    # --- append (called from any thread, never blocks on I/O) -------------

    def append_message(self, msg: Message) -> None:
        self.buffer.append(self.tables.qualify(table_for(msg)), to_row(msg))

    def append_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        qualified = self.tables.qualify(table)
        for row in rows:
            self.buffer.append(qualified, row)

    #: How many messages one `pull()` call takes from the cursor before
    #: checking whether more arrived. Generous on purpose: this only bounds
    #: one Python-level loop, not a wire frame (contrast `bus.py`'s
    #: `DEFAULT_BACKFILL_LIMIT`), so there is no reason to make more than one
    #: round trip through the loop for the common case.
    PULL_BATCH = 10_000

    def pull(self, cursor: StreamCursor) -> int:
        """Drain everything currently available from `cursor` into the
        per-table buffers -- envelope messages' route to the durable path,
        now decided here rather than at `emit()` time (see `shared.tables`).

        Loops rather than taking once: `cursor.take()` is bounded per call,
        and a run that has produced more than one `PULL_BATCH` since the
        flusher's last tick must not leave a remainder un-pulled until the
        next one -- `flushed_through_seq` reads `buffer.min_pending_seq()`,
        which only knows about rows that made it into the buffer, so a
        message still sitting only in the stream would be invisible to it
        and the mark could be reported *ahead* of a message that has not
        even been queued for writing yet.

        Result rows never reach here: `emit("result", rows=[...])` routes
        those straight to `append_rows`, seq-less and outside the stream by
        design (see `job/stream.py`'s module docstring).
        """
        pulled = 0
        while True:
            taken = cursor.take(self.PULL_BATCH)
            if not taken:
                return pulled
            for msg in taken:
                self.append_message(msg)
            pulled += len(taken)

    # --- flush (any thread; blocking, and left that way) ------------------

    def flush_due(self) -> int:
        """Write whatever has crossed the size or age bound. Nothing else."""
        due = self.buffer.due(max_bytes=self.max_bytes, max_age_s=self.max_age_s)
        return self._flush(due)

    def flush_all(self) -> int:
        """End of run: every table, both bounds ignored."""
        with self._flush_lock:
            pending = self.buffer.take_all()
            written = 0
            for table, rows in pending.items():
                written += self._write(table, rows)
            return written

    def _flush(self, tables: list[str]) -> int:
        if not tables:
            return 0
        with self._flush_lock:
            written = 0
            for table in tables:
                rows = self.buffer.take(table)
                written += self._write(table, rows)
            return written

    def _write(self, table: str, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        try:
            # Blocking, and deliberately still blocking: every caller is
            # already off the event loop — the flusher thread, or the runner's
            # one `to_thread` hop at teardown.
            count = self.writer.write_batch(table, rows)
        except Exception as exc:  # noqa: BLE001 - every failure mode is "retry later"
            self.write_failures += 1
            self.last_error = f"{table}: {exc}"
            self.buffer.restore(table, rows)
            log.warning("durable write to %s failed (%s); %d rows requeued", table, exc, len(rows))
            return 0
        self.rows_written += count
        return count

    def flushed_through_seq(self, issued: int) -> int:
        """The highest seq the durable store can definitely serve.

        ``issued`` is how many seqs have been handed out, so ``issued - 1`` is
        the newest message that exists. With nothing pending, all of it is
        durable; otherwise the answer stops one below the oldest row still
        waiting — see ``DurableBuffer.min_pending_seq``.
        """
        pending = self.buffer.min_pending_seq()
        return issued - 1 if pending is None else pending - 1

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


class DurableFlusher:
    """The periodic flush, on a daemon thread of its own.

    This was an ``asyncio.Task`` on the run's loop. It is a thread now for the
    reason at the top of this module: the durable path is the floor, and the
    floor must not stop when the loop does.

    Two details are the whole lifecycle, and both prevent a specific failure:

    - **The stop signal is a ``threading.Event``, waited on as the tick.**
      ``Event.wait(tick_s)`` returns the instant ``stop()`` fires, where a
      ``time.sleep(tick_s)`` loop that checks a flag afterwards would add a
      whole tick of latency to every run's teardown — a 30s flush interval
      would mean up to 30s of it.
    - **The thread is a daemon, and ``stop()`` still joins it.** Daemon so a
      thread that somehow outlives its stop can never hold the process open;
      joined anyway so the run's final flush is the last word on what is
      durable, rather than racing a tick nobody is waiting for.

    ``after_flush`` runs on this thread, right after the flush, and is where
    the run's stream learns how far Delta has caught up
    (``RunStream.note_flushed``). It is a callback rather than a ``RunStream``
    reference so the durable path keeps knowing nothing about replay or
    eviction — and ``RunStream`` guards every field with its own lock, so
    being called from here is safe.

    ``cursor``, when given, is pulled (``DurableSink.pull``) at the top of
    every tick, before ``flush_due`` looks at what has crossed a bound —
    envelope messages now reach the buffer this way rather than being pushed
    in at ``emit()`` time. ``None`` is a legitimate choice, not a placeholder
    to fill in later: a sink fed only through ``append_rows``/``append_message``
    directly (every test in ``test_flush_rules.py``, and any future caller
    with no ``RunStream`` at all) has nothing to pull and must not need one to
    exist.
    """

    def __init__(
        self,
        sink: DurableSink,
        *,
        tick_s: float,
        cursor: StreamCursor | None = None,
        after_flush: Callable[[], None] | None = None,
        name: str = "durable-flush",
    ) -> None:
        self.sink = sink
        self.tick_s = max(float(tick_s), MIN_TICK_S)
        self.cursor = cursor
        self.after_flush = after_flush
        self.name = name
        #: Ticks completed, error or not. The one observable that says the
        #: thread is really running, without a test having to guess a sleep.
        self.ticks = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("flusher already started")
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()

    def stop(self, timeout_s: float = STOP_TIMEOUT_S) -> None:
        """Signal, then join. Idempotent — teardown paths call it once each."""
        self._stop.set()
        thread = self._thread
        if thread is None:
            return
        # The handle is kept rather than dropped, so `running` can still answer
        # honestly if the join below times out. Clearing it here would report a
        # thread that is very much alive as stopped.
        thread.join(timeout_s)
        if thread.is_alive():
            # Only reachable through a write that never returns. Say so: the
            # alternative is a silent thread still writing after the run it
            # belonged to reported its outcome.
            log.warning("durable flush thread still running %.0fs after stop", timeout_s)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # A context manager because the commonest way to leak one of these is a
    # test (or a caller) that starts it and then fails before its stop.
    def __enter__(self) -> DurableFlusher:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def _run(self) -> None:
        while not self._stop.wait(self.tick_s):
            self._tick()

    def _tick(self) -> None:
        self.ticks += 1
        try:
            # This order is the whole safety property, and nothing type-checks
            # it: `pull` only TAKES from the stream (a message can be handed
            # here and then lost if the process dies before the write below),
            # so `after_flush` -- which is what tells `RunStream.note_flushed`
            # a seq is safe to evict -- must run strictly after `flush_due`
            # has actually attempted the write, never merely after `pull`. It
            # is safe here because both are synchronous, in this order, on
            # this thread: `flush_due` has either written a row out of the
            # buffer or put it back (`DurableBuffer.restore`, on failure)
            # before this line runs, so `after_flush` -> `flushed_through_seq`
            # -> `buffer.min_pending_seq()` only ever sees the post-attempt
            # state, never a row that was merely pulled.
            if self.cursor is not None:
                self.sink.pull(self.cursor)
            self.sink.flush_due()
            if self.after_flush is not None:
                self.after_flush()
        except Exception:  # noqa: BLE001 - a bad tick must not end the durable path
            # Keep ticking. The rows are still in the buffer, the next tick
            # retries them, and a thread that died here would take the whole
            # durable path down without anything noticing until the run ended.
            log.exception("durable flush tick raised; continuing")
