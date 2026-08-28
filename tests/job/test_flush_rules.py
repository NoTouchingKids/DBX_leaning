"""Flush on size OR age OR end-of-run — whichever fires first.

The age bound is the one that caps data loss on a crash. Size alone is not a
durability guarantee: a slow run may never reach 1 MB.
"""

from __future__ import annotations

import asyncio
import time

from job.buffer import DurableBuffer
from job.delta import JsonlWriter
from job.shared.envelope import make_message
from job.shared.tables import TableSet
from job.sink import DurableSink


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


async def test_end_of_run_flushes_everything_regardless(tmp_path):
    writer = JsonlWriter(tmp_path)
    sink = DurableSink(writer, TableSet(), max_bytes=10**9, max_age_s=10**9)
    sink.append_rows("t", [{"i": i} for i in range(3)])
    assert await sink.flush_due() == 0  # neither threshold reached
    assert await sink.flush_all() == 3
    assert len(writer.read_all("main.dbx_leaning.t")) == 3


async def test_a_failed_write_requeues_rather_than_losing_rows():
    writer = FlakyWriter(fail_times=1)
    sink = DurableSink(writer, TableSet(), max_bytes=1, max_age_s=0)
    sink.append_rows("t", [{"i": 1}, {"i": 2}])

    assert await sink.flush_due() == 0
    assert sink.write_failures == 1
    assert sink.unflushed == 2, "rows must not vanish on a failed write"
    assert not sink.healthy

    assert await sink.flush_due() == 2  # retried on the next tick
    assert sink.healthy and sink.rows_written == 2
    assert [r["i"] for r in writer.rows] == [1, 2], "order survived the retry"


async def test_restored_rows_keep_their_place_ahead_of_newer_ones():
    writer = FlakyWriter(fail_times=1)
    sink = DurableSink(writer, TableSet(), max_bytes=1, max_age_s=0)
    sink.append_rows("t", [{"i": 1}])
    await sink.flush_due()  # fails, requeues {'i': 1}
    sink.append_rows("t", [{"i": 2}])
    await sink.flush_due()
    assert [r["i"] for r in writer.rows] == [1, 2]


async def test_flush_loop_ticks_on_age_alone(tmp_path):
    from job.config import JobConfig
    from job.runner import JobHarness

    cfg = JobConfig(
        run_id="r",
        model_spec="tests.job.conftest:FakeModel",
        writer="jsonl",
        local_root=str(tmp_path),
        flush_tick_s=0.02,
        flush_max_age_s=0.02,
        flush_max_bytes=10**9,
    )
    harness = JobHarness(cfg)
    writer = JsonlWriter(tmp_path)
    sink = DurableSink(writer, TableSet(), max_bytes=10**9, max_age_s=0.02)
    sink.append_rows("t", [{"i": 1}])

    task = asyncio.create_task(harness._flush_loop(sink))
    await asyncio.sleep(0.15)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert sink.rows_written == 1, "the age bound never fired"


# --- how far the durable store has really caught up ------------------------


async def test_the_durable_high_water_mark_stops_below_the_lowest_pending_row(tmp_path):
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

    assert await sink.flush_due() == 5, "only the logs crossed the size bound"
    assert sink.buffer.min_pending_seq() == 0, "the progress row is still waiting"
    assert sink.flushed_through_seq(issued=6) == -1

    await sink.flush_all()
    assert sink.buffer.min_pending_seq() is None
    assert sink.flushed_through_seq(issued=6) == 5


async def test_result_rows_carry_no_seq_and_do_not_drag_the_mark_backwards(tmp_path):
    """They are model data, not envelope messages. Reading a missing `seq` as
    zero would pin the mark at -1 for every run that streams results."""
    sink = DurableSink(JsonlWriter(tmp_path), TableSet(), max_bytes=10**9, max_age_s=10**9)
    for seq in range(3):
        sink.append_message(make_message("log", run_id="r", seq=seq, message="x"))
    await sink.flush_all()

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
