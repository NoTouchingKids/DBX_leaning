"""Orchestration: load a model, drive it, get everything out.

The invariant this file exists to hold: **the job is autonomous, the backend
is an optional observer.** No app, an unreachable app, an app that appears
halfway through — all of them produce the same run and the same durable
record. Only the live commentary differs.

What changed when the transport collapsed to one socket:

- There is no relay and no channel list. One `WebSocketBus`, or None.
- The job keeps a `RunRecord` of its own status and recent messages, so it
  can *answer* as well as emit — a BACKFILL is served from memory instead of
  waking the SQL warehouse.
- Status transitions are reported to Lakebase by the job itself, so
  `run_status` stays current even for a run no socket ever attached to.
- Teardown **drains before it closes**. The old order closed the channels and
  then let the queue drain into them, which for a run that finishes faster
  than the socket can flush meant losing the whole live stream, terminal
  status included.
- The durable path runs on a thread of its own (`DurableFlusher`), not as a
  task on this loop. Delta is the floor, and a floor that stops ticking
  whenever the loop is wedged is not one. This file keeps exactly one hop
  into it, at teardown, so a Spark write taking seconds cannot stall the
  drain that follows.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from .auth import AppCredential
from .bus import WebSocketBus
from .cancellation import CancellationToken
from .config import JobConfig
from .delta import BatchWriter, select_writer
from .drivers import select_driver
from .emitter import Emitter
from .lakebase import LakebaseStatus
from .loader import ModelHandle, load_model
from .record import RunRecord
from .shared.envelope import RunStatus
from .shared.seq import SeqCounter
from .shared.tables import TableSet
from .sink import DurableFlusher, DurableSink

log = logging.getLogger(__name__)

__all__ = ["RunOutcome", "run_job", "JobHarness"]


@dataclass
class RunOutcome:
    run_id: str
    status: RunStatus
    detail: str | None = None
    seq_issued: int = 0
    rows_written: int = 0
    result_rows: int = 0
    result_chunks: int = 0
    live_sent: int = 0
    live_dropped: int = 0
    #: Left unsent when the drain deadline expired. Durable and BACKFILL-able,
    #: so this is a latency figure, not a loss figure.
    live_undrained: int = 0
    #: Lost to a socket that died mid-batch, as opposed to `live_dropped`,
    #: which is the queue shedding under backpressure. Separate because one
    #: says the connection failed and the other says the design worked.
    live_send_failures: int = 0
    backfills_served: int = 0
    status_reports: int = 0
    write_failures: int = 0
    unflushed_rows: int = 0
    observed_live: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


class JobHarness:
    """One run. Constructed in ``main()``; one process, one run."""

    def __init__(
        self,
        cfg: JobConfig,
        *,
        writer: BatchWriter | None = None,
        bus: WebSocketBus | None = None,
        status_reporter: LakebaseStatus | None = None,
        handle: ModelHandle | None = None,
    ) -> None:
        self.cfg = cfg
        self.tables = TableSet(catalog=cfg.catalog, schema=cfg.schema)
        self.token = CancellationToken()
        self.seq = SeqCounter()
        self.record = RunRecord(
            cfg.run_id,
            model=_model_name(cfg.model_spec),
            job_run_id=cfg.job_run_id,
            replay_messages=cfg.live_queue_max,
        )
        self._writer = writer
        self._bus = bus
        self._bus_injected = bus is not None
        self._status_reporter = status_reporter
        self._reporter_injected = status_reporter is not None
        self._handle = handle
        self.outcome: RunOutcome | None = None

    # --- assembly ---------------------------------------------------------

    def _credential(self) -> AppCredential:
        """One credential for both outbound callers — the token exchange is
        per principal, not per connection."""
        if not hasattr(self, "_cred"):
            self._cred = AppCredential(host=self.cfg.workspace_host)
        return self._cred

    def _build_bus(self) -> WebSocketBus | None:
        if self._bus_injected:
            # An injected bus was built before this harness existed, so it may
            # be holding a different RunRecord. There is exactly one record per
            # run and the backfill answers come out of it, so the harness's
            # wins — otherwise a test (or a caller assembling its own bus)
            # gets a socket that serves an empty replay ring and no error.
            if self._bus is not None:
                self._bus.record = self.record
            return self._bus
        ws_url = self.cfg.ws_url
        if not self.cfg.app_url or ws_url is None:
            # Normal case, not an error: apps run ~8h/day, jobs do not.
            log.info("no DBX_APP_URL — running unobserved, durable path only")
            return None
        return WebSocketBus(
            ws_url,
            self.cfg.run_id,
            record=self.record,
            token=self.cfg.app_token,
            credential=self._credential(),
            on_cancel=lambda who: self.token.cancel(f"cancelled by {who}"),
            next_seq=lambda: self.seq.issued,
            reconnect_s=self.cfg.ws_reconnect_s,
            ping_s=self.cfg.ws_ping_s,
            queue_max=self.cfg.live_queue_max,
            batch_max=self.cfg.ws_send_batch,
        )

    def _build_status_reporter(self) -> LakebaseStatus | None:
        if self._reporter_injected:
            return self._status_reporter
        if not self.cfg.lakebase_rest_url:
            log.info("no DBX_LAKEBASE_REST_URL — run_status is not reported live from here")
            return None
        return LakebaseStatus(
            self.cfg.lakebase_rest_url,
            schema=self.cfg.lakebase_schema,
            credential=self._credential(),
            timeout_s=self.cfg.http_timeout_s,
        )

    def _results_table(self, handle: ModelHandle) -> str:
        if self.cfg.results_table:
            return self.cfg.results_table
        if handle.results_table:
            return handle.results_table
        return f"results_{_model_name(self.cfg.model_spec)}"

    # --- the run ----------------------------------------------------------

    async def run(self) -> RunOutcome:
        cfg = self.cfg
        writer = self._writer or select_writer(cfg.writer, local_root=cfg.local_root)
        sink = DurableSink(
            writer,
            self.tables,
            max_bytes=cfg.flush_max_bytes,
            max_age_s=cfg.flush_max_age_s,
        )
        bus = self._build_bus()
        reporter = self._build_status_reporter()

        handle = self._handle or load_model(cfg.model_spec, cfg.model_config)
        emitter = Emitter(
            cfg.run_id,
            sink=sink,
            record=self.record,
            bus=bus,
            seq=self.seq,
            results_table=self._results_table(handle),
            preview_axes=handle.preview_axes,
            loop=asyncio.get_running_loop(),
        )
        handle.wire(emitter.emit, self.token)

        if bus is not None:
            await bus.start()
        # The periodic flush, and the one place the record learns how far the
        # warehouse has caught up — which is half of what a client needs to
        # decide whether to ask the job or ask SQL. On its own thread, so
        # neither depends on this loop getting scheduled.
        flusher = DurableFlusher(
            sink,
            tick_s=cfg.flush_tick_s,
            after_flush=lambda: self._note_flushed(sink),
        )
        flusher.start()

        status, detail = RunStatus.FAILED, None
        undrained = 0
        try:
            emitter.emit("status", status=RunStatus.RUNNING, detail="run started")
            await self._report(reporter)
            emitter.emit(
                "log",
                message=(
                    f"harness up: model={handle.describe()} writer={writer.name} "
                    f"bus={'websocket' if bus is not None else 'none'}"
                ),
                source="job",
                phase="input",
            )

            status, detail = await self._drive(handle, emitter)
        finally:
            # Everything below must happen whatever went wrong above.
            #
            # The flush thread is stopped FIRST, and the order is load-bearing
            # twice over. `stop()` joins, and a join blocks this loop — put it
            # after `_finalise` and that block lands between the terminal
            # status being queued and `bus.drain()`, which is the worst place
            # for it. And stopping here leaves `_finalise` as the only thing
            # touching the sink, so the "unflushed rows means not SUCCEEDED"
            # decision is made against a buffer nothing else can move.
            flusher.stop()
            status, detail = await self._finalise(sink, emitter, status, detail)
            await self._report(reporter)
            if bus is not None:
                # Drain FIRST, close second. The other order is what dropped
                # a fast run's whole live stream, terminal status included.
                undrained = await bus.drain(cfg.ws_drain_s)
                await bus.close()
            if reporter is not None and not self._reporter_injected:
                await reporter.close()
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                log.debug("writer close failed", exc_info=True)

        self.outcome = RunOutcome(
            run_id=cfg.run_id,
            status=status,
            detail=detail,
            seq_issued=self.seq.issued,
            rows_written=sink.rows_written,
            result_rows=emitter.result_rows_accepted,
            result_chunks=emitter.result_chunks,
            live_sent=bus.sent if bus is not None else 0,
            live_dropped=bus.dropped if bus is not None else 0,
            live_send_failures=bus.send_failures if bus is not None else 0,
            live_undrained=undrained,
            backfills_served=bus.backfills_served if bus is not None else 0,
            status_reports=reporter.writes if reporter is not None else 0,
            write_failures=sink.write_failures,
            unflushed_rows=sink.unflushed,
            observed_live=bool(bus is not None and bus.sent > 0),
        )
        return self.outcome

    def _note_flushed(self, sink: DurableSink) -> None:
        """How far Delta has caught up, told to the record.

        Runs on the flusher's thread, and again on the loop at the end of
        `_finalise`. Safe from both: `RunRecord` guards every field with its
        own lock, and the mark it keeps is a high-water mark, so two threads
        reporting out of order cannot move it backwards.
        """
        self.record.note_flushed(sink.flushed_through_seq(self.seq.issued))

    async def _report(self, reporter: LakebaseStatus | None) -> None:
        """Push the current status to Lakebase. Never load-bearing."""
        if reporter is None:
            return
        try:
            await reporter.report(self.record)
        except Exception:  # noqa: BLE001 - a status report is not the run
            log.debug("status report raised", exc_info=True)

    async def _drive(self, handle: ModelHandle, emitter: Emitter) -> tuple[RunStatus, str | None]:
        if handle.build is not None:
            await asyncio.to_thread(handle.build)
            # A model may only produce its solver object during build().
            handle.refresh()

        driver = select_driver(handle, emitter.emit, self.token)
        try:
            # The blocking call goes off-loop so the WebSocket keeps breathing.
            result = await asyncio.to_thread(driver.run)
            status, detail = result.status, result.detail
        except Exception as exc:  # noqa: BLE001 - the model failing is a run outcome
            log.exception("model raised")
            emitter.emit(
                "log", message=f"model raised: {exc!r}", level="ERROR", source="job", phase="run"
            )
            status, detail = RunStatus.FAILED, f"{type(exc).__name__}: {exc}"
        else:
            await self._collect_results(handle, emitter)

        if self.token.is_cancelled():
            # A cancelled run is a clean outcome, not a failure — and it keeps
            # whatever results it produced.
            status = RunStatus.CANCELLED
            detail = self.token.reason or detail
        return status, detail

    async def _collect_results(self, handle: ModelHandle, emitter: Emitter) -> None:
        """Pull results from the model, unless it already streamed them.

        A model that emits result chunks itself (rolling backtest, chunked
        inference) has already said everything it has to say; calling its
        results accessor again would double-write.
        """
        if emitter.result_chunks > 0 or handle.results is None:
            return
        rows = await asyncio.to_thread(handle.results)
        rows = list(rows or [])
        emitter.emit("result", rows=rows, final=True)

    async def _finalise(
        self, sink: DurableSink, emitter: Emitter, status: RunStatus, detail: str | None
    ) -> tuple[RunStatus, str | None]:
        """Flush, then decide the terminal status — in that order.

        A run must never report SUCCEEDED over a lost durable write. Flushing
        first is what makes that check possible rather than hopeful.
        """
        try:
            # The run's one hop to a thread, rather than one per write: the
            # sink is synchronous now, and this flush carries everything the
            # run produced, so on Spark it is the one that can take seconds.
            # Off-loop is what keeps the socket breathing through it.
            await asyncio.to_thread(sink.flush_all)
        except Exception:  # noqa: BLE001
            log.exception("final flush raised")

        if sink.unflushed > 0 and status is RunStatus.SUCCEEDED:
            lost = sink.unflushed
            status = RunStatus.FAILED
            detail = (
                f"durable write failed: {lost} row(s) unwritten after final flush "
                f"({sink.last_error}). Refusing to report SUCCEEDED over a lost write."
            )
            log.error(detail)

        try:
            emitter.emit("status", status=status, detail=detail)
            # Inline, NOT through `to_thread`, and this is the one place in the
            # teardown where that matters. The flush above already took
            # everything the run produced; what is left here is exactly one
            # row — the terminal status. Sending it through a thread would buy
            # nothing and cost a guaranteed yield, because `to_thread` does a
            # full executor round trip even with no rows to write.
            #
            # The yield it would create used to be actively dangerous: it
            # landed between `offer()`ing the terminal status and `bus.drain()`,
            # and drain tested `self._q` to decide whether to wait — which a
            # batch already in flight leaves empty, so it returned at once and
            # `close()` cancelled the send task mid-batch. ~45% of fast runs
            # lost the tail of their live stream that way. `drain()` waits on
            # `_idle` now and survives the yield, so this is no longer load-
            # bearing; it stays because a thread round trip for one row is
            # still pure cost.
            sink.flush_all()
        except Exception:  # noqa: BLE001
            log.exception("could not record terminal status")
        self._note_flushed(sink)
        return status, detail


def _model_name(spec: str) -> str:
    """``job.models.scenario:build_model`` -> ``scenario``."""
    return spec.split(":")[0].rstrip(".").split(".")[-1]


async def run_job(cfg: JobConfig, **kwargs: Any) -> RunOutcome:
    return await JobHarness(cfg, **kwargs).run()
