from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest

from job.config import JobConfig
from job.delta import JsonlWriter
from job.shared.envelope import Message
from job.shared.protocol import ControlFrame, ControlKind, Frame, unpack_frame


class FakeModel:
    """A self-driving model, the way a real one looks to the harness.

    No base class, no imports from the platform — exactly what
    docs/architecture.md describes.
    """

    results_table = "results_fake"
    preview_axes = ("t", "v")

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self.steps = int(cfg.get("steps", 5))
        self.step_sleep = float(cfg.get("step_sleep", 0.0))
        self.rows: list[dict[str, Any]] = []
        self.built = False
        self.emit = None
        self.should_cancel = None

    def build(self) -> None:
        self.built = True

    def run(self) -> None:
        for i in range(self.steps):
            if self.should_cancel():
                self.emit("log", message=f"stopping at step {i}", source="model", phase="run")
                return
            self.rows.append({"t": i, "v": i * 2.0})
            self.emit(
                "progress",
                elapsed_seconds=float(i),
                percent_complete=100.0 * (i + 1) / self.steps,
                primary_metric=float(i),
                primary_metric_label="step",
            )
            if self.step_sleep:
                time.sleep(self.step_sleep)

    def results(self) -> list[dict[str, Any]]:
        return self.rows


class BlockingModel:
    """Blocks until cancelled, polling at a known interval."""

    results_table = "results_blocking"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self.poll_s = float(cfg.get("poll_s", 0.01))
        self.timeout_s = float(cfg.get("timeout_s", 5.0))
        self.observed_at: float | None = None
        self.started = threading.Event()
        self.emit = None
        self.should_cancel = None

    def run(self) -> None:
        self.started.set()
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            if self.should_cancel():
                self.observed_at = time.monotonic()
                return
            time.sleep(self.poll_s)
        raise AssertionError("cancellation was never observed")

    def results(self) -> list[dict[str, Any]]:
        return [{"partial": True}]


class ChunkedModel:
    """Emits results incrementally, several times per run."""

    results_table = "results_chunked"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self.chunks = int(cfg.get("chunks", 3))
        self.per_chunk = int(cfg.get("per_chunk", 4))
        self.cancel_after = cfg.get("cancel_after")
        self.emit = None
        self.should_cancel = None

    def run(self) -> None:
        for c in range(self.chunks):
            if self.should_cancel():
                return
            rows = [{"chunk": c, "i": i, "v": float(c * 10 + i)} for i in range(self.per_chunk)]
            self.emit("result", rows=rows, final=(c == self.chunks - 1))


class FakeSocket:
    """As much of a websocket as ``job.bus.WebSocketLike`` asks for.

    Two things here that no earlier live-channel fake had, both load-bearing:

    - **``send`` actually awaits.** The old fakes returned without ever
      suspending, so the send pump never yielded, every queued message went
      out in one uninterrupted pass, and a teardown that closed the socket
      before the queue drained still looked correct. That is the bug this
      fake exists to be able to see.
    - **Inbound arrives through a queue**, so a test can deliver a frame
      *after* the bus has connected — which is what a BACKFILL request really
      looks like.
    """

    def __init__(
        self,
        inbound: list[bytes] | None = None,
        *,
        send_delay_s: float = 0.0,
        fail_on_send: bool = False,
    ) -> None:
        self.sent: list[bytes] = []
        self.send_delay_s = send_delay_s
        self.fail_on_send = fail_on_send
        self.closed = False
        self.opened = 0
        self._inbound: asyncio.Queue[bytes | None] = asyncio.Queue()
        for frame in inbound or []:
            self._inbound.put_nowait(frame)

    def push(self, frame: bytes) -> None:
        """Deliver an inbound frame to a bus that is already connected."""
        self._inbound.put_nowait(frame)

    # --- what the bus sent, decoded ---------------------------------------

    def frames(self) -> list[Frame]:
        return [unpack_frame(raw) for raw in self.sent]

    def messages(self) -> list[Message]:
        return [f for f in self.frames() if not isinstance(f, ControlFrame)]

    def control(self, kind: ControlKind | None = None) -> list[ControlFrame]:
        frames = [f for f in self.frames() if isinstance(f, ControlFrame)]
        return [f for f in frames if kind is None or f.kind is kind]

    # --- the WebSocketLike surface ----------------------------------------

    async def send(self, data: bytes) -> None:
        if self.fail_on_send:
            raise ConnectionError("socket gone")
        if self.send_delay_s:
            await asyncio.sleep(self.send_delay_s)
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True
        self._inbound.put_nowait(None)  # ends the inbound iterator

    async def __aenter__(self) -> FakeSocket:
        self.opened += 1
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        while True:
            frame = await self._inbound.get()
            if frame is None:
                return
            yield frame


def connector(ws: FakeSocket):
    """``connect=`` for a bus: hand it this socket, ignore the URL."""

    def _connect(url, **kwargs):
        return ws

    return _connect


async def until(predicate, timeout_s: float = 2.0) -> bool:
    """Poll until ``predicate()`` holds. Bounded, so a broken test fails fast
    rather than hanging, and no test has to guess a sleep long enough."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


@pytest.fixture
def writer(tmp_path):
    return JsonlWriter(tmp_path / "delta")


@pytest.fixture
def cfg(tmp_path):
    def _make(**overrides):
        base = dict(
            run_id="run-test",
            model_spec="tests.job.conftest:FakeModel",
            model_config={},
            app_url=None,
            catalog="main",
            schema="dbx_leaning",
            writer="jsonl",
            local_root=str(tmp_path / "delta"),
            # Fast enough that a test relying on the default never has to
            # sleep to see a flush happen -- since routing to the durable
            # buffer moved from push (at `emit()`) to pull (the flusher's own
            # cursor, `job/sink.py::DurableSink.pull`), a message's age clock
            # no longer starts until a tick has actually picked it up, so the
            # tick interval and the age bound now both sit on the critical
            # path to "durably confirmed" in a way the old push-based design
            # did not. Anything that needs to assert a flush has NOT yet
            # happened overrides these explicitly with slower values instead
            # (see e.g. test_flush_rules.py, and the size/age-parametrised
            # and NoisyLogger cases in test_transport_behaviour.py).
            flush_tick_s=0.001,
            flush_max_age_s=0.001,
        )
        base.update(overrides)
        return JobConfig(**base)

    return _make
