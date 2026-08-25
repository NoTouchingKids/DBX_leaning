"""Flush on size OR age OR end-of-run — whichever fires first.

The age bound is the one that caps data loss on a crash. Size alone is not a
durability guarantee: a slow run may never reach 1 MB.
"""

from __future__ import annotations

import asyncio
import time

from job.buffer import DurableBuffer
from job.delta import JsonlWriter
from job.sink import DurableSink
from shared.tables import TableSet


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


# --- writer selection ------------------------------------------------------


def test_delta_rs_refuses_rather_than_writing_to_a_local_directory():
    """write_deltalake() takes a URI, not a UC name — and given a three-part
    name it does not raise, it creates a local directory of that literal name.
    A deployed run would report SUCCEEDED with its telemetry in a container
    filesystem that is about to vanish. Refusing is the only honest option
    until credential vending exists."""
    import pytest

    from job.delta import DELTA_RS_UNIMPLEMENTED, DeltaRsWriter, select_writer

    with pytest.raises(NotImplementedError, match="not implemented"):
        DeltaRsWriter()
    with pytest.raises(NotImplementedError):
        select_writer("delta-rs")
    assert "credential vending" in DELTA_RS_UNIMPLEMENTED


def test_auto_never_picks_delta_rs():
    """Picking it automatically is exactly how the silent-local-write returns."""
    import pytest

    from job.delta import select_writer

    # No Spark session here and no local opt-in, so auto has nothing to choose
    # — and the error must name Spark, not fall through to delta-rs.
    with pytest.raises(RuntimeError, match="no active Spark session"):
        select_writer("auto")


def test_auto_falls_to_local_jsonl_only_behind_an_explicit_opt_in(monkeypatch, tmp_path):
    from job.delta import select_writer

    monkeypatch.setenv("DBX_ALLOW_LOCAL_WRITER", "1")
    writer = select_writer("auto", local_root=str(tmp_path))
    assert writer.name == "jsonl"


def test_an_unknown_writer_is_rejected_by_name():
    import pytest

    from job.delta import select_writer

    with pytest.raises(ValueError, match="expected auto|delta-rs|spark|jsonl"):
        select_writer("parquet")


def test_the_writer_kinds_are_an_enum_and_the_error_lists_them_all():
    """The valid set exists once.

    `select_writer` used to compare against four string literals and then
    report the valid set from a hand-written message — two lists kept in step
    by hand. Deriving the message from the enum is what stops them drifting,
    the same way `make_message` derives its error from `MessageType`.

    It matters more here than the size suggests: this selector chooses the
    DURABLE write path, and choosing wrong is the failure that already
    happened once — delta-rs given a three-part UC name wrote to a local
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
    assert WriterKind.parse("Delta-RS") is WriterKind.DELTA_RS
