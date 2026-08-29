"""The one boundary that makes this harness correct or subtly broken:
a model's callback fires on a worker thread, not the event loop.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from job.buffer import DurableBuffer
from job.bus import WebSocketBus
from job.delta import JsonlWriter
from job.emitter import Emitter
from job.record import RunRecord
from job.shared.envelope import MessageType
from job.shared.tables import TableSet
from job.sink import DurableSink
from job.stream import RunStream


def make_emitter(tmp_path, **kw):
    sink = DurableSink(JsonlWriter(tmp_path), TableSet(), buffer=DurableBuffer())
    record = RunRecord("run-1")
    stream = RunStream("run-1")
    bus = WebSocketBus(
        "ws://x/ws", "run-1", record=record, stream=stream, queue_max=kw.get("queue_max", 2000)
    )
    emitter = Emitter(
        "run-1",
        sink=sink,
        record=record,
        stream=stream,
        bus=bus,
        results_table=kw.get("results_table", "results_t"),
        preview_axes=kw.get("preview_axes"),
    )
    return emitter, sink, bus


async def test_burst_from_a_worker_thread_arrives_intact(tmp_path):
    # asyncio.Queue is not thread-safe; this is the crossing that replaces it.
    # Nothing crosses but a wake-up now (`bus.notify`, no payload) -- the data
    # itself already landed in `emitter.stream`, synchronously, on the calling
    # thread, before the hop is ever scheduled. So this checks two things
    # rather than one: the hop fires for every message (`notified`), and what
    # it is telling the loop to look at is actually complete and in order
    # (`emitter.stream`).
    emitter, sink, bus = make_emitter(tmp_path, queue_max=100_000)
    emitter.bind_loop(asyncio.get_running_loop())

    notified: list[int] = []
    bus.notify = lambda: notified.append(1)  # type: ignore[assignment]

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
        if len(notified) == N * THREADS:
            break

    assert len(notified) == N * THREADS, "the wake-up hop did not fire for every message"
    # The stream is written on the emitting thread too, so a BACKFILL arriving
    # mid-burst answers for everything that already exists.
    messages, complete = emitter.stream.read(-1)
    assert complete is True
    assert [m.seq for m in messages] == list(range(N * THREADS)), (
        "seq collided, duplicated, or went missing crossing into the loop"
    )
    # Envelope messages route to the write buffer by being pulled off a
    # cursor now (`DurableSink.pull`), not pushed at `emit()` time -- nothing
    # here ever pulls, so the buffer stays empty. `sink.buffer.appended`
    # counting durability was the old (pre-stream) design; `emitter.stream`
    # is durability now, checked above.
    assert sink.buffer.appended == 0
    assert emitter.record.counts()["log"] == N * THREADS


async def test_the_stream_append_and_the_record_both_precede_the_live_handoff(tmp_path):
    # Durability must not sit behind the droppable live path, and the record
    # has to be true the instant a message exists — it is what
    # `WebSocketBus._force_terminal` reads at teardown, and the loop may not
    # get round to the live hop for a while.
    emitter, sink, bus = make_emitter(tmp_path)
    emitter.bind_loop(asyncio.get_running_loop())
    order: list[str] = []

    original_append = emitter.stream.append
    emitter.stream.append = lambda m: (order.append("stream"), original_append(m))[1]  # type: ignore
    observe = emitter.record.observe
    emitter.record.observe = lambda m: (order.append("record"), observe(m))[1]  # type: ignore
    bus.notify = lambda: order.append("live")  # type: ignore[assignment]

    await asyncio.to_thread(emitter.emit, "log", message="x")
    for _ in range(20):
        await asyncio.sleep(0.005)
        if "live" in order:
            break
    assert order == ["stream", "record", "live"]


def test_emitting_without_a_loop_still_works(tmp_path):
    # A model's own unit tests, or a fully synchronous harness, must not need
    # a running loop just to call emit().
    emitter, sink, _ = make_emitter(tmp_path)
    msg = emitter.emit("log", message="no loop here")
    assert msg.seq == 0
    assert len(emitter.stream) == 1


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
    emitter, sink, bus = make_emitter(tmp_path)

    def explode(*args, **kwargs):
        raise RuntimeError("something is on fire")

    bus.notify = explode  # type: ignore[assignment]
    emitter.record.observe = explode  # type: ignore[assignment]
    emitter.stream.append = explode  # type: ignore[assignment]

    msg = emitter.emit("log", message="still fine")
    assert msg.type is MessageType.LOG


def test_client_invisible_logs_still_enter_the_stream_the_same_as_any_other_message(tmp_path):
    """`client_visible=False` is a live-SEND filter, and that filtering now
    happens where the live send happens — `WebSocketBus._send_loop`, see
    `test_bus.py::test_a_client_invisible_log_is_never_sent_live`. The
    emitter's own job stops at appending both to the stream: everything a
    model emits is durability-safe (retained until confirmed written)
    regardless of visibility, which is what the durable write path — and a
    BACKFILL's raw cursor for the durable side, `StreamCursor.take` — both
    rely on being true.
    """
    emitter, sink, bus = make_emitter(tmp_path)
    emitter.emit("log", message="raw solver chatter", client_visible=False)
    emitter.emit("log", message="shown", client_visible=True)

    messages = emitter.stream.cursor().take(10)
    assert [m.message for m in messages] == ["raw solver chatter", "shown"]
