"""``emit(type, **fields)`` — the model's entire coupling surface.

This is the boundary the whole harness turns on. The model's blocking call
runs in a thread executor so the event loop stays free to hold a WebSocket
open; every ``emit`` therefore arrives **on a worker thread, not the loop**.

Two rules follow, and they are the reason this class exists:

1. ``asyncio.Queue`` is not thread-safe, so the live hand-off crosses via
   ``loop.call_soon_threadsafe``. Three lines of stdlib, no dependency.
2. The stream append happens *synchronously, on the calling thread*, before
   anything else. Durability must not sit behind the same droppable,
   eventually-consistent path as the live socket — that is what makes "logs
   may drop live, never durably" true rather than aspirational.

**What "durable" means changed shape, not strength.** It used to mean
"pushed into the write buffer" directly. Now it means "appended to
``RunStream``" — and that is *stronger*, not weaker: ``job/stream.py``'s
eviction rule refuses to drop a message until the durable sink has pulled
*and* confirmed writing it (see that module's docstring), so nothing appended
here can vanish before Delta has it. The write itself still happens later, off
this thread, on the flusher's own cursor — what changes at the instant
``append`` returns is that losing it has become impossible, not that it has
already reached Delta.

The run record is written on the same thread and for the same reason: it is
what a status message needs updated the instant it exists (``summary()`` for
Lakebase; the terminal status for ``WebSocketBus._force_terminal`` at
teardown), not once the event loop gets round to it.

Order is the whole design: **stream, then record, then live.** Each step is
allowed to fail without the ones before it being undone.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from typing import Any

from .bus import WebSocketBus
from .record import RunRecord
from .shared.downsample import downsample_rows
from .shared.envelope import PREVIEW_MAX_POINTS, Message, MessageType, make_message
from .shared.seq import SeqCounter
from .sink import DurableSink
from .stream import RunStream

log = logging.getLogger(__name__)

__all__ = ["Emitter"]


class Emitter:
    def __init__(
        self,
        run_id: str,
        *,
        sink: DurableSink,
        record: RunRecord,
        stream: RunStream | None = None,
        bus: WebSocketBus | None = None,
        seq: SeqCounter | None = None,
        results_table: str | None = None,
        preview_axes: tuple[str, str] | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        preview_points: int = 500,
    ) -> None:
        self.run_id = run_id
        self.sink = sink
        self.record = record
        #: Every seq'd message's one durability-safe home — see the module
        #: docstring. Optional only so a model's own unit tests (or a bare
        #: `Emitter` in isolation) need not construct one by hand first;
        #: `JobHarness` always supplies the run's real stream.
        self.stream = stream if stream is not None else RunStream(run_id)
        #: None when the run is unobserved — no `DBX_APP_URL`, or no live
        #: channel wanted. Not an error: apps run ~8h/day, jobs do not.
        self.bus = bus
        self.seq = seq or SeqCounter()
        self.results_table = results_table
        self.preview_axes = preview_axes
        self.preview_points = min(preview_points, PREVIEW_MAX_POINTS)
        self._loop = loop

        self._result_chunks = 0
        self._result_rows_accepted = 0
        self._lock = threading.Lock()
        self.on_message: Callable[[Message], None] | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # --- the callback a model is handed ----------------------------------

    def emit(self, type: str, **fields: Any) -> Message:
        """Stamp ``run_id``/``seq``/``ts`` and fan out. Safe from any thread.

        Never raises into model code for a *transport* problem — a model
        should not have to care whether anyone is listening. It does raise on
        a malformed message, because that is the model's own bug and silently
        swallowing it is how message shapes drift.
        """
        if type == MessageType.RESULT.value or type is MessageType.RESULT:
            fields = self._absorb_result_rows(fields)

        # One seq per message, consumed even for messages that will be
        # filtered off the live path — gaps are never renumbered around.
        msg = make_message(type, run_id=self.run_id, seq=self.seq.next(), **fields)

        try:
            # The one synchronous, thread-safe write everything else rests
            # on — see the module docstring for why this is durability now,
            # not merely a cache the durable path happens to read.
            self.stream.append(msg)
        except Exception:  # noqa: BLE001 - durability failing must not kill the run
            log.exception("stream append failed for seq=%s; durability at risk", msg.seq)

        try:
            self.record.observe(msg)
        except Exception:  # noqa: BLE001 - the record is an aid, not the truth
            log.exception("run record rejected seq=%s", msg.seq)

        self._offer_live()

        if self.on_message is not None:
            try:
                self.on_message(msg)
            except Exception:  # noqa: BLE001 - observers are never load-bearing
                log.exception("on_message observer raised")
        return msg

    __call__ = emit

    # --- results ----------------------------------------------------------

    def _absorb_result_rows(self, fields: dict[str, Any]) -> dict[str, Any]:
        """Take ``rows=[...]`` off a result message and route it durably.

        A model calls ``emit("result", rows=[...])`` once (or once per chunk).
        The rows go to the model's own results table; the *message* keeps only
        a preview, a row count and a fetch hint — the wire contract never
        carries the result set itself.
        """
        fields = dict(fields)
        rows = fields.pop("rows", None)
        if rows is None:
            return fields

        rows = list(rows)
        declared = fields.get("row_count")
        if declared is not None and int(declared) != len(rows):
            raise ValueError(
                f"result declares row_count={declared} but carries {len(rows)} rows; "
                "omit row_count and let the harness count them"
            )

        with self._lock:
            chunk = self._result_chunks
            self._result_chunks += 1
            self._result_rows_accepted += len(rows)

        table = self.results_table
        if table is None:
            raise ValueError(
                "model emitted result rows but no results table is configured; "
                "set DBX_RESULTS_TABLE or expose `results_table` on the model"
            )

        stamped = [{"run_id": self.run_id, "chunk_index": chunk, **row} for row in rows]
        self.sink.append_rows(table, stamped)

        fields.setdefault("chunk_index", chunk)
        fields["row_count"] = len(rows)
        fields.setdefault("fetch_hint", {"table": self.sink.tables.qualify(table), "key": "run_id"})
        if "preview" not in fields:
            x, y = self.preview_axes or (None, None)
            fields["preview"] = downsample_rows(rows, self.preview_points, x=x, y=y)
        return fields

    @property
    def result_rows_accepted(self) -> int:
        with self._lock:
            return self._result_rows_accepted

    @property
    def result_chunks(self) -> int:
        with self._lock:
            return self._result_chunks

    # --- live hand-off ----------------------------------------------------

    def _offer_live(self) -> None:
        """Wake the bus: the message is already in `self.stream` for it to
        read. Never raises: a model must not learn that nobody is listening,
        and the stream append already happened regardless.

        No payload crosses here any more — unlike the old push queue, there
        is nothing left to hand over. The thread hop is still required
        (`asyncio.Event` is no more thread-safe than `asyncio.Queue` was),
        it just now carries a wake-up instead of data.
        """
        if self.bus is None:
            return
        loop = self._loop
        if loop is None:
            self._notify_now()  # no loop bound: single-threaded/test use
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._notify_now()
            return
        try:
            loop.call_soon_threadsafe(self._notify_now)
        except RuntimeError:
            # Loop already closed — the run is ending; durable write stands.
            pass

    def _notify_now(self) -> None:
        if self.bus is None:
            return
        try:
            self.bus.notify()
        except Exception:  # noqa: BLE001 - the live path is never load-bearing
            log.exception("live bus notify failed; stream copy stands")
