"""One run, on threads.

The invariant this file exists to hold is v3's and has not changed: **the job
is autonomous, the app is an optional observer.** No app, an unreachable app,
an app that appears halfway through — all produce the same run and the same
durable record. Only the live commentary differs.

What has changed is that there is no event loop. v3 was asyncio, and paid for
it in one specific place: a serverless `spark_python_task` runs inside an
ipykernel that already owns a running loop, so `asyncio.run` refused outright
and `job/main.py` grew a ThreadPoolExecutor and a twenty-line comment to work
around it. Threads delete that problem rather than routing around it — the
model blocks on the main thread, which is what a solver actually is, and the
socket and the roller each get one of their own.

Three threads, and that is the whole concurrency story:

    main      the model, blocking, exactly as it wants to be
    roller    rolls telemetry parts on size OR age (job/telemetry.py)
    socket    the RPC client, when there is an app to talk to (job/ws.py)

They meet at two places only: a `queue.Queue` of outbound frames, and the
`CancellationToken`, which was already a `threading.Event` in v3 and needed no
change at all.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from shared.envelope import RunStatus, make_message
from shared.seq import SeqCounter

from .cancellation import CancellationToken
from .loader import ModelHandle, load_model
from .status import NullStatusWriter, StatusWriter
from .telemetry import TelemetryWriter

log = logging.getLogger(__name__)

__all__ = ["Channel", "RunOutcome", "Harness"]

#: The live sink: handed every stamped record, after the durable write.
#: Best-effort by contract — it must return promptly and a raise is swallowed.
Channel = Callable[[dict[str, Any]], None]


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
    extra: dict[str, Any] = field(default_factory=dict)


class Harness:
    """Drives one model, gets its messages onto the durable path, and offers
    them to a live channel if one is attached.

    **Its parts are slots.** Each is a public attribute, settable as a keyword
    at construction and replaceable by plain assignment until `run()` starts
    (and refused after). That is what makes "change the harness freely, as
    long as the wire contract holds" a supported path rather than a fork:

    ``writer``         the durable path — `TelemetryWriter`, normally
                       `PartFileWriter`. Every record goes here first.
    ``channel``        the live sink, a `Channel` callable, or None for nobody
                       listening (the normal case, not a degraded one).
    ``token``          the `CancellationToken` the model polls and a cancel
                       sets.
    ``seq``            the run's `SeqCounter` — one counter, every type.
    ``handle``         the `ModelHandle` to drive. None means "load
                       ``model_spec`` with ``model_config`` when run() starts".
    ``status_writer``  where every `status` message ALSO goes: the Lakebase
                       `run_status` writer, a `StatusWriter`. Defaults to
                       `NullStatusWriter`. See `job/status.py` for its contract.

    The live channel is injected rather than constructed here: it keeps this
    file free of any transport, and it is what lets the whole run be exercised
    with nothing listening — which is the normal case, not a degraded one.
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
    ) -> None:
        if channel is not None and on_message is not None:
            raise TypeError("pass `channel` or its older name `on_message`, not both")
        self._running = False
        self.run_id = run_id
        self.model_spec = model_spec
        self.model_config = model_config or {}

        self.writer = writer
        #: Where a live message goes, if anything is listening. Best-effort by
        #: contract: this must never raise into a run, and a run with no
        #: listener is not degraded. `on_message` is the name it had before it
        #: was a slot, kept as a constructor alias.
        self.channel = channel if channel is not None else on_message
        self.token = token if token is not None else CancellationToken()
        self.seq = seq if seq is not None else SeqCounter()
        self.handle = handle
        self.status_writer = status_writer if status_writer is not None else NullStatusWriter()

        self._roll_tick_s = roll_tick_s
        self._live_offered = 0
        self._stop_roller = threading.Event()

    # --- the callback a model is handed ------------------------------------

    def emit(self, type: str, **fields: Any) -> None:
        """`emit(type, **fields)` — the model's entire coupling surface.

        Stamps `run_id`/`seq`/`ts`, writes durably, then offers it live. In
        that order, deliberately: the durable path is the floor, and a live
        channel that is slow or broken must not be able to lose a record or
        delay one reaching the volume.
        """
        message = make_message(type, run_id=self.run_id, seq=self.seq.next(), **fields)
        record = message.model_dump(mode="json")

        self.writer.append(record)

        if self.channel is not None:
            try:
                self.channel(record)
                self._live_offered += 1
            except Exception:  # noqa: BLE001 - a dead channel is not a failed run
                log.debug("live channel refused a message; the run is unaffected", exc_info=True)

        if record["type"] == "status":
            self._write_status(record)

    def _write_status(self, record: dict[str, Any]) -> None:
        """Hand a status record to the `status_writer` slot. Never raises."""
        writer = self.status_writer
        if isinstance(writer, NullStatusWriter):
            return
        try:
            landed = writer.write(
                record["run_id"],
                record["seq"],
                record["status"],
                bool(record.get("terminal", False)),
                record.get("detail"),
                record["ts"],
            )
        except Exception:  # noqa: BLE001 - run_status is at-most-stale, never load-bearing
            log.warning("status writer raised; run_status may be stale", exc_info=True)
            return
        if landed is False:
            log.info("status writer did not land seq=%s; run_status may be stale", record["seq"])

    def _close_status_writer(self) -> None:
        close = getattr(self.status_writer, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                log.debug("status writer close raised", exc_info=True)

    # --- the run -----------------------------------------------------------

    def run(self) -> RunOutcome:
        self._running = True
        roller = threading.Thread(target=self._roll_loop, name="roller", daemon=True)
        roller.start()

        status, terminal, detail = RunStatus.FAILED, True, None
        try:
            handle = self.handle or load_model(self.model_spec, self.model_config)
            handle.wire(self.emit, self.token)

            self.emit("status", status=RunStatus.RUNNING, terminal=False, detail="run started")
            self.emit(
                "log",
                message=f"harness up: model={handle.describe()} writer=parts",
                source="job",
                phase="input",
            )
            status, detail = self._drive(handle)
            terminal = True
        except Exception as exc:  # noqa: BLE001 - a model failing is an outcome
            log.exception("model raised")
            self.emit(
                "log",
                message=f"model raised: {exc!r}",
                level="ERROR",
                source="job",
                phase="run",
            )
            status, detail = RunStatus.FAILED, f"{type(exc).__name__}: {exc}"
        finally:
            self._stop_roller.set()
            roller.join(timeout=5)
            status, detail = self._finalise(status, detail)

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
        )

    def _drive(self, handle: ModelHandle) -> tuple[str, str | None]:
        if handle.build is not None:
            handle.build()
            handle.refresh()

        if handle.run is None:
            raise RuntimeError(f"model {handle.spec} has nothing to run: {handle.describe()}")

        # Blocking, on this thread. No `asyncio.to_thread`, because there is no
        # loop to keep breathing — the socket has its own thread and is
        # unaffected by however long the model takes.
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

    def _finalise(self, status: str, detail: str | None) -> tuple[str, str | None]:
        """Close the durable path, THEN decide the terminal status.

        That order is the whole point. A run must never report SUCCEEDED over
        a lost write, and only a flush that has already happened can be
        checked. `unflushed` is exact here rather than approximate: on a UC
        volume nothing is durable until its part closes.
        """
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

        try:
            self.emit("status", status=status, terminal=True, detail=detail)
            self.writer.close()  # the terminal status itself must land
        except Exception:  # noqa: BLE001
            log.exception("could not record terminal status")
        self._close_status_writer()
        return status, detail

    def _roll_loop(self) -> None:
        """Age parts out on a timer.

        Without this a slow run sits below any size cap indefinitely and
        nothing becomes durable — size alone is not a durability guarantee,
        which is why the age bound exists and why it is the real bound on what
        a crash loses.
        """
        while not self._stop_roller.wait(self._roll_tick_s):
            try:
                self.writer.roll_if_due()
            except Exception:  # noqa: BLE001 - the next tick tries again
                log.exception("roll failed; records stay pending")

    # --- what the app can ask, once there is a socket ----------------------

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
