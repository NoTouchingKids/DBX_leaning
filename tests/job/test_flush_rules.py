"""Flush on size OR age OR end-of-run — whichever fires first.

The age bound is the one that caps data loss on a crash. Size alone is not a
durability guarantee: a slow run may never reach 1 MB.

These are synchronous because the durable path is. It was async once, and the
only thing the event loop ever did on this path was hand a blocking write back
to a thread. Now that it is gone, the property worth testing is the one its
absence buys: the flush keeps happening when the loop is wedged.
"""

from __future__ import annotations

import threading
import time

from job.buffer import DurableBuffer
from job.delta import JsonlWriter
from job.shared.envelope import make_message
from job.shared.tables import TableSet
from job.sink import DurableFlusher, DurableSink
from job.stream import RunStream


class FlakyWriter:
    name = "flaky"

    def __init__(self, fail_times: int = 1):
        self.fail_times = fail_times
        self.calls: list[tuple[str, int]] = []
        self.rows: list[dict] = []

    def write_batch(self, table, rows):
        self.calls.append((table, len(rows)))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("delta unavailable")
        self.rows.extend(rows)
        return len(rows)

    def close(self): ...


def wait_until(predicate, timeout_s: float = 2.0) -> bool:
    """Poll until ``predicate()`` holds, on the calling thread.

    Blocking on purpose — the thing under test runs on a thread of its own, so
    a test asserting on it must not need the event loop to make progress. The
    bound is what stops a broken flusher hanging the suite instead of failing.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_size_threshold_fires_on_its_own():
    buf = DurableBuffer()
    for i in range(50):
        buf.append("t", {"i": i, "pad": "x" * 100})
    assert buf.due(max_bytes=1_000, max_age_s=999) == ["t"]
    assert buf.due(max_bytes=10**9, max_age_s=999) == []


def test_age_threshold_fires_on_its_own():
    buf = DurableBuffer()
    buf.append("t", {"i": 1})
    assert buf.due(max_bytes=10**9, max_age_s=999) == []
    time.sleep(0.05)
    assert buf.due(max_bytes=10**9, max_age_s=0.01) == ["t"]


def test_tables_flush_independently():
    buf = DurableBuffer()
    buf.append("big", {"pad": "x" * 5000})
    buf.append("small", {"i": 1})
    assert buf.due(max_bytes=1000, max_age_s=999) == ["big"]
    assert buf.take("big") and buf.stats().rows == 1  # 'small' untouched


def test_age_clock_restarts_after_a_flush():
    buf = DurableBuffer()
    buf.append("t", {"i": 1})
    time.sleep(0.03)
    buf.take("t")
    buf.append("t", {"i": 2})
    assert buf.due(max_bytes=10**9, max_age_s=0.02) == []


def test_end_of_run_flushes_everything_regardless(tmp_path):
    writer = JsonlWriter(tmp_path)
    sink = DurableSink(writer, TableSet(), max_bytes=10**9, max_age_s=10**9)
    sink.append_rows("t", [{"i": i} for i in range(3)])
    assert sink.flush_due() == 0  # neither threshold reached
    assert sink.flush_all() == 3
    assert len(writer.read_all("main.dbx_leaning.t")) == 3


def test_a_failed_write_requeues_rather_than_losing_rows():
    writer = FlakyWriter(fail_times=1)
    sink = DurableSink(writer, TableSet(), max_bytes=1, max_age_s=0)
    sink.append_rows("t", [{"i": 1}, {"i": 2}])

    assert sink.flush_due() == 0
    assert sink.write_failures == 1
    assert sink.unflushed == 2, "rows must not vanish on a failed write"
    assert not sink.healthy

    assert sink.flush_due() == 2  # retried on the next tick
    assert sink.healthy and sink.rows_written == 2
    assert [r["i"] for r in writer.rows] == [1, 2], "order survived the retry"


def test_restored_rows_keep_their_place_ahead_of_newer_ones():
    writer = FlakyWriter(fail_times=1)
    sink = DurableSink(writer, TableSet(), max_bytes=1, max_age_s=0)
    sink.append_rows("t", [{"i": 1}])
    sink.flush_due()  # fails, requeues {'i': 1}
    sink.append_rows("t", [{"i": 2}])
    sink.flush_due()
    assert [r["i"] for r in writer.rows] == [1, 2]


# --- the flush thread ------------------------------------------------------
#
# Every test here starts the flusher through `with`, so a failed assertion
# cannot leave a daemon thread ticking into the rest of the suite.


def test_the_flush_thread_ticks_on_age_alone(tmp_path):
    writer = JsonlWriter(tmp_path)
    sink = DurableSink(writer, TableSet(), max_bytes=10**9, max_age_s=0.02)
    sink.append_rows("t", [{"i": 1}])

    with DurableFlusher(sink, tick_s=0.02):
        assert wait_until(lambda: sink.rows_written == 1), "the age bound never fired"

    assert len(writer.read_all("main.dbx_leaning.t")) == 1


async def test_the_durable_path_keeps_flushing_while_the_event_loop_is_blocked(tmp_path):
    """The whole reason this stopped being an asyncio task.

    Delta is the floor, not a fallback tier, and a floor that stops ticking
    whenever the loop is wedged is not one. This test is a coroutine and then
    blocks its own thread, which is exactly what a long synchronous callback
    does to the loop: nothing async can run for the duration. The old
    `asyncio.sleep`-driven flush loop would have written nothing here.
    """
    writer = JsonlWriter(tmp_path)
    sink = DurableSink(writer, TableSet(), max_bytes=10**9, max_age_s=0.01)
    sink.append_rows("t", [{"i": 1}])

    with DurableFlusher(sink, tick_s=0.01):
        assert wait_until(lambda: sink.rows_written == 1), "the durable path stalled with the loop"


def test_the_flush_thread_tells_the_stream_how_far_delta_has_caught_up(tmp_path):
    """Flushing is only half the tick. The other half is the number that goes
    out on `hello` and on every backfill reply — without it a client cannot
    tell whether to ask the job or ask SQL. `RunStream` locks every field it
    owns, which is what makes writing it from this thread safe.

    Feeds the sink through `append_message` directly rather than a stream
    cursor (no `cursor=` given to `DurableFlusher`), the same way every other
    test in this file does — this is about `after_flush` reaching the stream,
    not about the pull step, which `test_transport_behaviour.py` covers via a
    real run.
    """
    stream = RunStream("r")
    sink = DurableSink(JsonlWriter(tmp_path), TableSet(), max_bytes=10**9, max_age_s=0.01)
    for seq in range(3):
        sink.append_message(make_message("log", run_id="r", seq=seq, message="x"))

    def note() -> None:
        stream.note_flushed(sink.flushed_through_seq(issued=3))

    assert stream.flushed_through_seq == -1
    with DurableFlusher(sink, tick_s=0.01, after_flush=note):
        assert wait_until(lambda: stream.flushed_through_seq == 2), (
            "the tick flushed but never told the stream"
        )


def test_stopping_the_flusher_does_not_wait_out_the_current_tick(tmp_path):
    """The stop signal is a `threading.Event` waited on *as* the tick.

    A `time.sleep(tick)` loop that only checks its stop flag afterwards adds a
    whole interval to every run's teardown — up to 30s at the default flush
    interval, for a run that had nothing left to write.
    """
    sink = DurableSink(JsonlWriter(tmp_path), TableSet())
    flusher = DurableFlusher(sink, tick_s=30.0)
    flusher.start()
    try:
        started = time.monotonic()
        flusher.stop()
        assert time.monotonic() - started < 1.0, "stop waited out the tick"
    finally:
        flusher.stop()  # idempotent; here so a failure above still cleans up
    assert not flusher.running


def test_a_stopped_flusher_leaves_no_thread_behind(tmp_path):
    """A daemon thread outliving its run is invisible until it hangs a test
    suite, or writes on behalf of a run that already reported its outcome."""
    sink = DurableSink(JsonlWriter(tmp_path), TableSet(), max_age_s=0.01)
    with DurableFlusher(sink, tick_s=0.01) as flusher:
        assert wait_until(lambda: flusher.ticks >= 1)

    assert not flusher.running
    assert [t.name for t in threading.enumerate() if t.name.startswith("durable-flush")] == []


def test_a_tick_that_raises_does_not_take_the_durable_path_down_with_it(tmp_path):
    """Same posture the asyncio loop had: log it and keep ticking. A thread
    that dies here takes durability with it and nothing notices until the run
    ends with a buffer full of rows."""
    writer = JsonlWriter(tmp_path)
    sink = DurableSink(writer, TableSet(), max_bytes=1, max_age_s=0)

    def boom():
        raise RuntimeError("the record went away")

    sink.append_rows("t", [{"i": 1}])
    with DurableFlusher(sink, tick_s=0.01, after_flush=boom) as flusher:
        assert wait_until(lambda: flusher.ticks >= 3), "the thread died on the first raise"
        sink.append_rows("t", [{"i": 2}])
        assert wait_until(lambda: sink.rows_written == 2), "flushing stopped after the raise"


def test_a_zero_tick_interval_is_floored_rather_than_spun(tmp_path):
    """`DBX_FLUSH_TICK_S=0` is one typo away in a deploy, and a zero wait does
    not tick — it burns a core for the length of the run on billed compute."""
    flusher = DurableFlusher(DurableSink(JsonlWriter(tmp_path), TableSet()), tick_s=0.0)
    assert flusher.tick_s > 0


# --- how far the durable store has really caught up ------------------------


def test_the_durable_high_water_mark_stops_below_the_lowest_pending_row(tmp_path):
    """Not the highest seq written — the lowest still waiting, minus one.

    Tables flush independently, so a `run_logs` flush must not claim that an
    unflushed `run_progress` row is durable. This number goes out on `hello`
    and on every backfill reply, and a client told the warehouse can serve
    seq 4 would go and fetch a row that is not there.
    """
    writer = JsonlWriter(tmp_path)
    sink = DurableSink(writer, TableSet(), max_bytes=500, max_age_s=999)

    sink.append_message(make_message("progress", run_id="r", seq=0, elapsed_seconds=1.0))
    for seq in range(1, 6):
        sink.append_message(make_message("log", run_id="r", seq=seq, message="x" * 200))

    assert sink.flush_due() == 5, "only the logs crossed the size bound"
    assert sink.buffer.min_pending_seq() == 0, "the progress row is still waiting"
    assert sink.flushed_through_seq(issued=6) == -1

    sink.flush_all()
    assert sink.buffer.min_pending_seq() is None
    assert sink.flushed_through_seq(issued=6) == 5


def test_result_rows_carry_no_seq_and_do_not_drag_the_mark_backwards(tmp_path):
    """They are model data, not envelope messages. Reading a missing `seq` as
    zero would pin the mark at -1 for every run that streams results."""
    sink = DurableSink(JsonlWriter(tmp_path), TableSet(), max_bytes=10**9, max_age_s=10**9)
    for seq in range(3):
        sink.append_message(make_message("log", run_id="r", seq=seq, message="x"))
    sink.flush_all()

    sink.append_rows("results_t", [{"a": 1}, {"a": 2}])

    assert sink.buffer.min_pending_seq() is None
    assert sink.flushed_through_seq(issued=3) == 2


# --- writer selection ------------------------------------------------------


def test_auto_refuses_rather_than_quietly_writing_somewhere_local():
    """The failure this selector exists to prevent already happened once: a
    writer handed a three-part UC name wrote to a local directory without
    erroring, so a run reported SUCCEEDED with its telemetry in a container
    about to be discarded. Failing loudly beats every silent fallback."""
    import pytest

    from job.delta import select_writer

    # No Spark session here and no local opt-in, so auto has nothing to choose
    # — and the error must say so rather than find something to write to.
    with pytest.raises(RuntimeError, match="no Spark session could be found or created"):
        select_writer("auto")


def test_auto_falls_to_local_jsonl_only_behind_an_explicit_opt_in(monkeypatch, tmp_path):
    from job.delta import select_writer

    monkeypatch.setenv("DBX_ALLOW_LOCAL_WRITER", "1")
    writer = select_writer("auto", local_root=str(tmp_path))
    assert writer.name == "jsonl"


def test_an_unknown_writer_is_rejected_by_name():
    import pytest

    from job.delta import select_writer

    # Escaped, so this asserts the whole list rather than matching whichever
    # alternative happens to appear.
    with pytest.raises(ValueError, match=r"expected one of auto\|spark\|jsonl"):
        select_writer("parquet")


def test_the_writer_kinds_are_an_enum_and_the_error_lists_them_all():
    """The valid set exists once.

    `select_writer` used to compare against string literals and then report
    the valid set from a hand-written message — two lists kept in step by
    hand. Deriving the message from the enum is what stops them drifting, the
    same way `make_message` derives its error from `MessageType`.

    It matters more here than the size suggests: this selector chooses the
    DURABLE write path, and choosing wrong is the failure that already
    happened once — a writer given a three-part UC name wrote to a local
    directory without erroring, so a run reported SUCCEEDED with its telemetry
    in a container about to be discarded.
    """
    import pytest

    from job.delta import WriterKind, select_writer

    with pytest.raises(ValueError) as excinfo:
        select_writer("sprak")

    message = str(excinfo.value)
    for kind in WriterKind:
        assert kind.value in message, f"{kind.value} missing from {message!r}"


def test_the_writer_kind_survives_what_an_env_var_really_contains():
    """`DBX_WRITER` is read straight from the environment, where a value
    arrives with whatever case and whitespace a deploy gave it."""
    from job.delta import WriterKind

    assert WriterKind.parse(None) is WriterKind.AUTO
    assert WriterKind.parse("") is WriterKind.AUTO
    assert WriterKind.parse("  SPARK ") is WriterKind.SPARK
    assert WriterKind.parse("Jsonl") is WriterKind.JSONL
