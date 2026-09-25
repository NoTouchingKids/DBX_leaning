"""One run, on four threads.

The invariant this file exists to hold is v3's and has not changed: **the job
is autonomous, the app is an optional observer.** No app, an unreachable app,
an app that appears halfway through — all produce the same run and the same
durable record. Only the live commentary differs.

There is no event loop. v3 was asyncio, and paid for it in one specific place:
a serverless `spark_python_task` runs inside an ipykernel that already owns a
running loop, so `asyncio.run` refused outright. Threads delete that problem
rather than routing around it. Multiprocessing and an async rewrite were both
considered again for v5 and rejected (docs/v5-implementation-plan.md, Phase 3
item 6): a model's own GIL-holding C extension is not fixed by more threads or
processes, and async only pays for itself with an event loop of its own.

Four threads, and what each is allowed to do:

    main        the model, blocking, exactly as it wants to be. `emit()` runs
                here, so it does only what is fast and cannot block on I/O:
                stamp, append to the writer (a lock and a list), hand the
                record to the live channel (a queue put), queue a status write.
    controller  everything else the harness does (job/controller.py): rolls
                telemetry parts on size OR age, runs every RPC handler —
                `cancel`, `replay`, `ping`, and `hello`'s answer — and every
                status-writer (Lakebase) call. FIFO, one item at a time.
    sender      the socket's outbound side (job/ws.py): connects, says
                `hello`, and — once the app accepts it — blocks on the outbox
                and sends. The only thread that ever calls `ws.send`.
    receiver    the socket's inbound side: blocks in `ws.recv`, parses, and
                hands every request (and `hello`'s answer) to the controller.
                Never runs a handler itself.

The last two exist only while there is an app to talk to. Where the four meet,
and nowhere else:

    SeqCounter          main stamps; sender reads `issued` for `hello`
    PartFileWriter      main appends; controller rolls, flushes, replays
    CancellationToken   controller (a `cancel` RPC) or a signal handler on
                        main sets it; the model on main polls it
    controller queue    main (status writes), receiver (RPC) -> controller
    outbox              main (records), controller (RPC replies) -> sender
    session             receiver (closed), controller (hello's answer) -> sender

Each has its own lock, and none is held across I/O except the roll gate here,
which is held across a roll on purpose (see `_shutdown`).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from shared.envelope import RunStatus, make_message
from shared.seq import SeqCounter

from .cancellation import CancellationToken
from .controller import Controller
from .loader import ModelHandle, load_model
from .status import NullStatusWriter, StatusWriter
from .telemetry import TelemetryWriter

log = logging.getLogger(__name__)

__all__ = ["Channel", "LiveChannel", "RunOutcome", "Harness"]

#: How long shutdown WAITS for the terminal status to reach the status writer
#: (Lakebase) before moving on to `bye`. Sized to the Lakebase writer's worst
#: case — two connects and two statements at 5s each — so a slow-but-healthy
#: reconnect still lands; past it, `run_status` is left at-most-stale and the
#: part files are, as always, the record.
DEFAULT_STATUS_TIMEOUT_S = 25.0

#: How long shutdown waits for the live channel to flush and say `bye`.
DEFAULT_CHANNEL_CLOSE_TIMEOUT_S = 5.0


class LiveChannel(Protocol):
    """A live sink with a lifecycle — `job/ws.py::RpcClient` is the one there is.

    `send` must return promptly and never block on I/O: it is called from
    `emit()`, on the model's thread. Two further methods are OPTIONAL and
    found by name: `start(executor)`, called at the top of `run()` with the
    controller's `submit` — where the channel must run its inbound handlers —
    and `close(timeout)`, called as the LAST step of shutdown, after the
    terminal status is durable and reported. See `Harness._shutdown`.
    """

    def send(self, record: dict[str, Any]) -> None: ...


#: What the `channel` slot takes: a `LiveChannel`, or any plain callable taking
#: one record (`print`, `list.append`) for a sink with no lifecycle.
Channel = LiveChannel | Callable[[dict[str, Any]], None]


class _Slot:
    """One named, swappable part of a `Harness`.

    Readable always; replaceable until `run()` starts, and refused after — a
    part swapped mid-run would be half the old one's and half the new one's,
    with nothing to say which record went where.
    """

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name
        self.attr = f"_slot_{name}"

    def __get__(self, obj: Any, objtype: type | None = None) -> Any:
        if obj is None:
            return self
        return getattr(obj, self.attr)

    def __set__(self, obj: Any, value: Any) -> None:
        if getattr(obj, "_running", False):
            raise RuntimeError(
                f"Harness.{self.name} cannot be replaced once run() has started; "
                f"assemble the harness first, then run it"
            )
        setattr(obj, self.attr, value)


@dataclass
class RunOutcome:
    run_id: str
    status: str
    terminal: bool = True
    detail: str | None = None
    seq_issued: int = 0
    rows_written: int = 0
    unflushed: int = 0
    write_failures: int = 0
    #: Messages HANDED to the live channel — not messages delivered. The
    #: channel queues and may never connect, so this is an offer count and is
    #: named like one. Whether anything arrived is the channel's to report.
    live_offered: int = 0
    #: Status-writer calls that landed / failed or raised / were withheld
    #: because their record was not in a closed part (see `_write_status`).
    status_writes: int = 0
    status_write_failures: int = 0
    status_writes_withheld: int = 0
    #: Whether shutdown saw the terminal status reach the status writer within
    #: `status_timeout_s`. True when there is no status writer at all.
    status_terminal_reported: bool = True
    #: `terminal: true` statuses a MODEL emitted, recorded as non-terminal.
    coerced_terminal: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


class Harness:
    """Drives one model, gets its messages onto the durable path, and offers
    them to a live channel and a status writer if either is attached.

    **Its parts are slots.** Each is a public attribute, settable as a keyword
    at construction and replaceable by plain assignment until `run()` starts
    (and refused after). That is what makes "change the harness freely, as
    long as the wire contract holds" a supported path rather than a fork:

    ``writer``         the durable path — `TelemetryWriter`, normally
                       `PartFileWriter`. Every record goes here first, on the
                       model's thread; durable writes never pass through a
                       queue that could drop them.
    ``channel``        the live sink — a `LiveChannel` (the socket) or a plain
                       callable — or None for nobody listening, which is the
                       normal case, not a degraded one.
    ``token``          the `CancellationToken` the model polls and a cancel
                       sets.
    ``seq``            the run's `SeqCounter` — one counter, every type.
    ``handle``         the `ModelHandle` to drive. None means "load
                       ``model_spec`` with ``model_config`` when run() starts".
    ``status_writer``  where every `status` message ALSO goes: the Lakebase
                       `run_status` writer, a `StatusWriter`. Defaults to
                       `NullStatusWriter`. Called on the controller, never on
                       the model's thread. See `job/status.py`.

    ``controller`` is not a slot: it is the harness's own thread, and a
    channel reaches it only through the executor `start()` hands over.
    """

    writer = _Slot()
    channel = _Slot()
    token = _Slot()
    seq = _Slot()
    handle = _Slot()
    status_writer = _Slot()

    def __init__(
        self,
        run_id: str,
        writer: TelemetryWriter,
        *,
        model_spec: str = "heartbeat",
        model_config: dict[str, Any] | None = None,
        handle: ModelHandle | None = None,
        channel: Channel | None = None,
        on_message: Channel | None = None,
        token: CancellationToken | None = None,
        seq: SeqCounter | None = None,
        status_writer: StatusWriter | None = None,
        roll_tick_s: float = 1.0,
        status_timeout_s: float = DEFAULT_STATUS_TIMEOUT_S,
        channel_close_timeout_s: float = DEFAULT_CHANNEL_CLOSE_TIMEOUT_S,
    ) -> None:
        if channel is not None and on_message is not None:
            raise TypeError("pass `channel` or its older name `on_message`, not both")
        self._running = False
        self.run_id = run_id
        self.model_spec = model_spec
        self.model_config = model_config or {}

        self.writer = writer
        #: `on_message` is the name this had before it was a slot, kept as a
        #: constructor alias.
        self.channel = channel if channel is not None else on_message
        self.token = token if token is not None else CancellationToken()
        self.seq = seq if seq is not None else SeqCounter()
        self.handle = handle
        self.status_writer = status_writer if status_writer is not None else NullStatusWriter()

        self.status_timeout_s = status_timeout_s
        self.channel_close_timeout_s = channel_close_timeout_s
        self.controller = Controller(tick_s=roll_tick_s, on_tick=self._tick)

        #: Held by the controller while it rolls the writer, and taken by
        #: shutdown to end controller rolls. See `_shutdown`, step 2.
        self._roll_gate = threading.Lock()
        self._rolls_open = True

        self._live_offered = 0
        self._coerced_terminal = 0
        self._status_writes = 0
        self._status_failures = 0
        self._status_withheld = 0

    # --- the callback a model is handed ------------------------------------

    def emit(self, type: str, **fields: Any) -> None:
        """`emit(type, **fields)` — the model's entire coupling surface.

        Stamps `run_id`/`seq`/`ts`, writes durably, then offers it live, then
        (for a `status`) queues it for the status writer. In that order,
        deliberately: the durable path is the floor, and nothing after it may
        lose a record or delay one reaching the volume. Nothing here does I/O:
        the live offer is a queue put and the status write runs on the
        controller.

        **A model cannot end the run.** A `status` with `terminal: true` is
        recorded with `terminal: false`, plus one warning per run. Coerced
        rather than refused: refusing would raise into the model and turn a
        harmless slip into a FAILED run — losing whatever it was about to
        produce — while the status name and detail it carried are still worth
        keeping. Coercing is what keeps "exactly one terminal status per run,
        the harness's, the last record" (the part-file contract) true of every
        run. A model that wants to name its outcome returns it from `run()`.
        """
        if type == "status" and fields.get("terminal"):
            fields["terminal"] = False
            self._coerced_terminal += 1
            record = self._emit(type, fields)
            if self._coerced_terminal == 1:
                warning = (
                    f"model emitted a terminal status ({record.get('status')!r}, "
                    f"seq={record['seq']}); only the harness ends a run, so it was "
                    f"recorded as non-terminal. Return the status from run() to make "
                    f"it the run's outcome."
                )
                log.warning(warning)
                self._emit(
                    "log",
                    {"message": warning, "level": "WARNING", "source": "job", "phase": "run"},
                )
            return
        self._emit(type, fields)

    def _emit(
        self, type: str, fields: dict[str, Any], *, report_status: bool = True
    ) -> dict[str, Any]:
        message = make_message(type, run_id=self.run_id, seq=self.seq.next(), **fields)
        record = message.model_dump(mode="json")

        self.writer.append(record)
        self._offer(record)
        if report_status and record["type"] == "status":
            self._queue_status(record)
        return record

    def _offer(self, record: dict[str, Any]) -> None:
        channel = self.channel
        if channel is None:
            return
        send = getattr(channel, "send", channel)
        try:
            send(record)
            self._live_offered += 1
        except Exception:  # noqa: BLE001 - a dead channel is not a failed run
            log.debug("live channel refused a message; the run is unaffected", exc_info=True)

    # --- the status writer, on the controller ------------------------------

    def _has_status_writer(self) -> bool:
        return not isinstance(self.status_writer, NullStatusWriter)

    def _queue_status(self, record: dict[str, Any]) -> None:
        if self._has_status_writer():
            self.controller.submit(lambda: self._write_status(record))

    def _write_status(self, record: dict[str, Any]) -> None:
        """Report one status to the status writer. Runs on the controller.

        **Never ahead of the volume.** The record is flushed into a closed part
        first — one roll, ~117ms on a UC volume, affordable for the handful of
        statuses a run has, and self-coalescing: statuses queued behind one
        flush find their records already closed — and if it still is not in
        one, it is withheld rather than reported. A crash at any point then
        leaves Lakebase at-most-stale relative to the part files, never the
        reverse. Once shutdown has closed the roll gate this does not roll:
        shutdown has closed the part the terminal status is in itself.
        """
        seq = record["seq"]
        with self._roll_gate:
            if self._rolls_open and self.writer.holds(seq):
                try:
                    self.writer.flush()
                except Exception:  # noqa: BLE001 - the check below decides
                    log.exception("flush before a status write raised")
        if self.writer.holds(seq):
            self._status_withheld += 1
            log.warning(
                "status seq=%s (%s) is not in a closed part yet; not reporting it to "
                "run_status — at-most-stale, never ahead of the volume",
                seq,
                record.get("status"),
            )
            return
        try:
            landed = self.status_writer.write(
                record["run_id"],
                seq,
                record["status"],
                bool(record.get("terminal", False)),
                record.get("detail"),
                record["ts"],
            )
        except Exception:  # noqa: BLE001 - run_status is at-most-stale, never load-bearing
            self._status_failures += 1
            log.warning("status writer raised; run_status may be stale", exc_info=True)
            return
        if landed is False:
            self._status_failures += 1
            log.info("status writer did not land seq=%s; run_status may be stale", seq)
        else:
            self._status_writes += 1

    def _close_status_writer(self) -> None:
        close = getattr(self.status_writer, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                log.debug("status writer close raised", exc_info=True)

    def _tick(self) -> None:
        """Age parts out on a timer — on the controller.

        Without this a slow run sits below any size cap indefinitely and
        nothing becomes durable — size alone is not a durability guarantee,
        which is why the age bound exists and why it is the real bound on what
        a crash loses.
        """
        with self._roll_gate:
            if not self._rolls_open:
                return
            try:
                self.writer.roll_if_due()
            except Exception:  # noqa: BLE001 - the next tick tries again
                log.exception("roll failed; records stay pending")

    # --- the run -----------------------------------------------------------

    def run(self) -> RunOutcome:
        self._running = True
        self.controller.start()
        self._start_channel()

        status, terminal, detail = RunStatus.FAILED, True, None
        reported = True
        try:
            handle = self.handle or load_model(self.model_spec, self.model_config)
            handle.wire(self.emit, self.token)

            self._emit(
                "status", {"status": RunStatus.RUNNING, "terminal": False, "detail": "run started"}
            )
            self._emit(
                "log",
                {
                    "message": f"harness up: model={handle.describe()} writer=parts",
                    "source": "job",
                    "phase": "input",
                },
            )
            status, detail = self._drive(handle)
            terminal = True
        except Exception as exc:  # noqa: BLE001 - a model failing is an outcome
            log.exception("model raised")
            self._emit(
                "log",
                {
                    "message": f"model raised: {exc!r}",
                    "level": "ERROR",
                    "source": "job",
                    "phase": "run",
                },
            )
            status, detail = RunStatus.FAILED, f"{type(exc).__name__}: {exc}"
        finally:
            status, detail, reported = self._shutdown(status, detail)

        return RunOutcome(
            run_id=self.run_id,
            status=status,
            terminal=terminal,
            detail=detail,
            seq_issued=self.seq.issued,
            rows_written=self.writer.rows_written,
            unflushed=self.writer.unflushed,
            write_failures=self.writer.write_failures,
            live_offered=self._live_offered,
            status_writes=self._status_writes,
            status_write_failures=self._status_failures,
            status_writes_withheld=self._status_withheld,
            status_terminal_reported=reported,
            coerced_terminal=self._coerced_terminal,
        )

    def _start_channel(self) -> None:
        start = getattr(self.channel, "start", None)
        if not callable(start):
            return
        try:
            start(self.controller.submit)
        except Exception:  # noqa: BLE001 - no live channel is the normal case
            log.warning("live channel failed to start; the run continues unobserved", exc_info=True)

    def _drive(self, handle: ModelHandle) -> tuple[str, str | None]:
        if handle.build is not None:
            handle.build()
            handle.refresh()

        if handle.run is None:
            raise RuntimeError(f"model {handle.spec} has nothing to run: {handle.describe()}")

        # Blocking, on this thread. No `asyncio.to_thread`, because there is no
        # loop to keep breathing — the socket and the controller have threads
        # of their own and are unaffected by however long the model takes.
        reported = handle.run()

        if self.token.is_cancelled():
            # A cancelled run is a clean outcome, not a failure, and it keeps
            # whatever it produced. This wins over whatever the model returned:
            # a model that noticed the cancel and one that did not must not
            # produce different terminal statuses for the same event.
            return RunStatus.CANCELLED, self.token.reason

        # A model may name its own status — that is what an open `status`
        # field is for. Anything falsy means "you decide", which is the common
        # case and what a model returning None gets.
        return (str(reported) if reported else RunStatus.SUCCEEDED), None

    def _shutdown(self, status: str, detail: str | None) -> tuple[str, str | None, bool]:
        """The terminal shutdown order — SIGNED OFF 2026-09-24. Do not reorder.

        1. **The model's own result write.** Already done when this runs: a
           model writes its results table and emits `result` inside `run()`,
           and every `result` it emitted is already appended to the writer. A
           cancelled or failed run keeps whatever it had written.
        2. **Part files flushed (closed).** Controller rolls are ended first —
           the roll gate waits out a roll in flight, so no other thread can be
           mid-write when `unflushed` is read — then the final close, THEN the
           terminal status is decided: SUCCEEDED only if nothing is unflushed,
           because only a flush that has already happened can be checked. The
           terminal status is emitted, and the writer closed AGAIN, because
           that status is itself a record and must land too.
        3. **Status writer (Lakebase) terminal write** — on the controller,
           behind anything already queued there, and waited for at most
           `status_timeout_s`. The writer is closed on the controller right
           after it. A timeout leaves `run_status` at-most-stale: the WAIT is
           bounded here; the write is the writer's own to bound.
        4. **`bye`, and the socket closed** — `channel.close()`. Last, so the
           socket outlives the model and the terminal status still has a live
           channel to travel on, and so the app hears "finished" only once
           there is something finished to read. Then the controller stops.

        A crash between any two steps leaves the part files as the floor and
        Lakebase at-most-stale, never the reverse. Between 1 and 2 there is no
        terminal status anywhere, which is how a reader tells "crashed" from
        "finished". Between 2 and 3 the volume has it and Lakebase does not
        yet. Between 3 and 4 both do, and the app sees a dropped socket rather
        than `bye` and backfills.
        """
        # --- 2. part files flushed; terminal status decided, emitted, closed
        with self._roll_gate:
            self._rolls_open = False
        try:
            self.writer.close()
        except Exception:  # noqa: BLE001
            log.exception("final roll raised")

        if self.writer.unflushed > 0 and status == RunStatus.SUCCEEDED:
            lost = self.writer.unflushed
            status = RunStatus.FAILED
            detail = (
                f"durable write failed: {lost} record(s) never reached the volume "
                f"({self.writer.last_error}). Refusing to report SUCCEEDED over a lost write."
            )
            log.error(detail)

        final: dict[str, Any] | None = None
        try:
            final = self._emit(
                "status",
                {"status": status, "terminal": True, "detail": detail},
                report_status=False,  # step 3, not now: it is not durable yet
            )
            self.writer.close()  # the terminal status itself must land
        except Exception:  # noqa: BLE001
            log.exception("could not record terminal status")

        # --- 3. status writer terminal write, waited for with a bound ------
        reported = True
        if self._has_status_writer():
            if final is not None:
                self._queue_status(final)
            self.controller.submit(self._close_status_writer)
            reported = self.controller.drain(self.status_timeout_s)
            if not reported:
                log.warning(
                    "the terminal status did not reach the status writer within %.1fs; "
                    "run_status is left at-most-stale (the part files have it)",
                    self.status_timeout_s,
                )

        # --- 4. bye ---------------------------------------------------------
        close = getattr(self.channel, "close", None)
        if callable(close):
            try:
                close(self.channel_close_timeout_s)
            except Exception:  # noqa: BLE001 - a courtesy, not a guarantee
                log.debug("live channel close raised", exc_info=True)
        self.controller.stop(timeout=1.0)
        return status, detail, reported

    # --- what the app can ask, once there is a socket ----------------------
    #
    # Both run on the controller when called over the socket: the receiver
    # hands every request to `controller.submit`, never to the model's thread.

    def replay(self, from_seq: int, to_seq: int | None = None) -> list[dict[str, Any]]:
        """Serve a gap from this run's own telemetry — closed parts AND
        pending. See `job/telemetry.py`; the pending half is the one that
        matters and the one an implementation forgets."""
        return self.writer.replay(from_seq, to_seq)

    def cancel(self, requested_by: str | None = None) -> dict[str, Any]:
        """Accept a cancel and say so. The acknowledgement v3 could not give:
        it set a flag and replied nothing, so the app could not tell
        'delivered' from 'lost'."""
        already = self.token.is_cancelled()
        self.token.cancel(f"cancelled by {requested_by or 'app'}")
        return {
            "accepted": True,
            "already_cancelling": already,
            "run_id": self.run_id,
            "at_seq": self.seq.issued,
            "ts": int(time.time() * 1000),
        }
