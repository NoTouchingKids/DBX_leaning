"""Orchestration: load a model, drive it, get everything out.

The invariant this file exists to hold: **the job is autonomous, the app is
an optional observer.** No app, an unreachable app, an app that appears
halfway through — all of them produce the same run and the same durable
record. Only the live commentary differs.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from shared.envelope import RunStatus
from shared.protocol import ControlFrame, ControlKind
from shared.seq import SeqCounter
from shared.tables import TableSet

from .cancellation import CancellationToken
from .channels import HttpPushChannel, WebSocketChannel
from .config import JobConfig
from .delta import BatchWriter, select_writer
from .drivers import select_driver
from .emitter import Emitter
from .loader import ModelHandle, load_model
from .relay import LiveChannel, LiveRelay
from .sink import DurableSink

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
        channels: list[LiveChannel] | None = None,
        handle: ModelHandle | None = None,
    ) -> None:
        self.cfg = cfg
        self.tables = TableSet(catalog=cfg.catalog, schema=cfg.schema)
        self.token = CancellationToken()
        self.seq = SeqCounter()
        self._writer = writer
        self._channels = channels
        self._handle = handle
        self.outcome: RunOutcome | None = None

    # --- assembly ---------------------------------------------------------

    def _build_channels(self) -> list[LiveChannel]:
        if self._channels is not None:
            return self._channels
        ws_url, push_url = self.cfg.ws_url, self.cfg.push_url
        if not self.cfg.app_url or ws_url is None or push_url is None:
            # Normal case, not an error: apps run ~8h/day, jobs do not.
            log.info("no DBX_APP_URL — running unobserved, durable path only")
            return []
        return [
            WebSocketChannel(
                ws_url,
                self.cfg.run_id,
                token=self.cfg.app_token,
                on_control=self._on_control,
                next_seq=lambda: self.seq.issued,
                reconnect_s=self.cfg.ws_reconnect_s,
                ping_s=self.cfg.ws_ping_s,
            ),
            HttpPushChannel(
                push_url,
                token=self.cfg.app_token,
                timeout_s=self.cfg.http_timeout_s,
            ),
        ]

    def _on_control(self, frame: ControlFrame) -> None:
        """Inbound from the app. Cancel is the only command that exists."""
        if frame.kind is ControlKind.CANCEL:
            who = frame.payload.get("requested_by") or "app"
            log.info("cancel requested by %s", who)
            self.token.cancel(f"cancelled by {who}")

    def _results_table(self, handle: ModelHandle) -> str:
        if self.cfg.results_table:
            return self.cfg.results_table
        if handle.results_table:
            return handle.results_table
        leaf = self.cfg.model_spec.split(":")[0].rstrip(".").split(".")[-1]
        return f"results_{leaf}"

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
        relay = LiveRelay(
            self._build_channels(),
            queue_max=cfg.live_queue_max,
            batch_max=cfg.http_push_batch,
        )

        handle = self._handle or load_model(cfg.model_spec, cfg.model_config)
        emitter = Emitter(
            cfg.run_id,
            sink=sink,
            relay=relay,
            seq=self.seq,
            results_table=self._results_table(handle),
            preview_axes=handle.preview_axes,
            loop=asyncio.get_running_loop(),
        )
        handle.wire(emitter.emit, self.token)

        await relay.start()
        pump = asyncio.create_task(relay.pump(), name="live-pump")
        flusher = asyncio.create_task(self._flush_loop(sink), name="flush-loop")

        status, detail = RunStatus.FAILED, None
        try:
            emitter.emit("status", status=RunStatus.RUNNING, detail="run started")
            emitter.emit(
                "log",
                message=f"harness up: model={handle.describe()} writer={writer.name}",
                source="job",
                phase="input",
            )

            status, detail = await self._drive(handle, emitter)
        finally:
            # Everything below must happen whatever went wrong above.
            status, detail = await self._finalise(sink, emitter, status, detail)
            flusher.cancel()
            await relay.stop()
            await asyncio.gather(pump, flusher, return_exceptions=True)
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
            live_sent=relay.sent,
            live_dropped=relay.dropped,
            write_failures=sink.write_failures,
            unflushed_rows=sink.unflushed,
            observed_live=relay.sent > 0,
        )
        return self.outcome

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
            await sink.flush_all()
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
            await sink.flush_all()  # the terminal status itself must land
        except Exception:  # noqa: BLE001
            log.exception("could not record terminal status")
        return status, detail

    async def _flush_loop(self, sink: DurableSink) -> None:
        try:
            while True:
                await asyncio.sleep(self.cfg.flush_tick_s)
                await sink.flush_due()
        except asyncio.CancelledError:
            raise


async def run_job(cfg: JobConfig, **kwargs: Any) -> RunOutcome:
    return await JobHarness(cfg, **kwargs).run()
