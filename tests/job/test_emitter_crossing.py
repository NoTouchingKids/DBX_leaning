"""The one boundary that makes this harness correct or subtly broken:
a model's callback fires on a worker thread, not the event loop.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from job.buffer import DurableBuffer
from job.delta import JsonlWriter
from job.emitter import Emitter
from job.relay import LiveRelay
from job.shared.envelope import LogMessage, MessageType
from job.shared.tables import TableSet
from job.sink import DurableSink


def make_emitter(tmp_path, **kw):
    sink = DurableSink(JsonlWriter(tmp_path), TableSet(), buffer=DurableBuffer())
    relay = LiveRelay([], **{k: v for k, v in kw.items() if k in {"queue_max"}})
    emitter = Emitter(
        "run-1",
        sink=sink,
        relay=relay,
        results_table=kw.get("results_table", "results_t"),
        preview_axes=kw.get("preview_axes"),
    )
    return emitter, sink, relay


async def test_burst_from_a_worker_thread_arrives_intact(tmp_path):
    # asyncio.Queue is not thread-safe; this is the crossing that replaces it.
    emitter, sink, relay = make_emitter(tmp_path, queue_max=100_000)
    emitter.bind_loop(asyncio.get_running_loop())

    received: list[int] = []
    relay.offer = lambda msg: received.append(msg.seq)  # type: ignore[assignment]

    N, THREADS = 500, 4
    done = threading.Barrier(THREADS + 1)

    def worker(w: int):
        for i in range(N):
            emitter.emit("log", message=f"w{w}-{i}", source="model")
        done.wait()

    for w in range(THREADS):
        threading.Thread(target=worker, args=(w,), daemon=True).start()

    await asyncio.to_thread(done.wait)
    # call_soon_threadsafe callbacks run on the next loop iterations.
    for _ in range(50):
        await asyncio.sleep(0.01)
        if len(received) == N * THREADS:
            break

    assert len(received) == N * THREADS, "messages were lost crossing into the loop"
    assert sorted(received) == list(range(N * THREADS)), "seq collided or duplicated"
    assert sink.buffer.appended == N * THREADS, "durable path lost messages"


async def test_durable_append_happens_before_the_live_handoff(tmp_path):
    # Durability must not sit behind the droppable live queue.
    emitter, sink, relay = make_emitter(tmp_path)
    emitter.bind_loop(asyncio.get_running_loop())
    order: list[str] = []

    original = sink.append_message
    sink.append_message = lambda m: (order.append("durable"), original(m))[1]  # type: ignore
    relay.offer = lambda m: order.append("live")  # type: ignore[assignment]

    await asyncio.to_thread(emitter.emit, "log", message="x")
    for _ in range(20):
        await asyncio.sleep(0.005)
        if "live" in order:
            break
    assert order == ["durable", "live"]


def test_emitting_without_a_loop_still_works(tmp_path):
    # A model's own unit tests, or a fully synchronous harness, must not need
    # a running loop just to call emit().
    emitter, sink, _ = make_emitter(tmp_path)
    msg = emitter.emit("log", message="no loop here")
    assert msg.seq == 0 and sink.buffer.appended == 1


def test_every_message_consumes_exactly_one_seq(tmp_path):
    emitter, _, _ = make_emitter(tmp_path)
    seqs = [
        emitter.emit("log", message="a").seq,
        emitter.emit("log", message="hidden", client_visible=False).seq,
        emitter.emit("progress", elapsed_seconds=1.0).seq,
        emitter.emit("status", status="RUNNING").seq,
    ]
    # The filtered-out log still consumes a value: gaps are never renumbered.
    assert seqs == [0, 1, 2, 3]


def test_a_malformed_message_raises_into_model_code(tmp_path):
    # A transport problem must not raise at a model. A model's own bad message
    # must, or shapes drift silently — which is the failure this contract exists
    # to prevent.
    from pydantic import ValidationError

    emitter, _, _ = make_emitter(tmp_path)
    with pytest.raises(ValidationError):
        emitter.emit("progress", elapsed_seconds=1.0, nonsense_field=1)


def test_transport_failure_never_reaches_the_model(tmp_path):
    emitter, sink, relay = make_emitter(tmp_path)

    def explode(msg):
        raise RuntimeError("relay is on fire")

    relay.offer = explode  # type: ignore[assignment]
    sink.append_message = lambda m: (_ for _ in ()).throw(RuntimeError("delta is on fire"))  # type: ignore

    msg = emitter.emit("log", message="still fine")
    assert msg.type is MessageType.LOG


async def test_client_invisible_logs_are_written_but_not_relayed(tmp_path):
    emitter, sink, relay = make_emitter(tmp_path)
    emitter.bind_loop(asyncio.get_running_loop())
    emitter.emit("log", message="raw solver chatter", client_visible=False)
    emitter.emit("log", message="shown", client_visible=True)

    pump = asyncio.create_task(relay.pump())
    sent: list[LogMessage] = []

    class Sink:
        name = "test"
        is_connected = True

        async def send_many(self, msgs):
            sent.extend(msgs)
            return True

        async def start(self): ...
        async def close(self): ...

    relay.channels.append(Sink())
    await asyncio.sleep(0.05)
    await relay.stop()
    await asyncio.gather(pump, return_exceptions=True)

    assert [m.message for m in sent] == ["shown"]
    assert sink.buffer.appended == 2, "durable path must keep both"
