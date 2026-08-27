"""End-to-end harness behaviour, with no Databricks connection anywhere."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from job.delta import JsonlWriter
from job.loader import describe_object
from job.runner import JobHarness
from job.shared.envelope import RunStatus

from .conftest import BlockingModel, ChunkedModel, FakeModel


def rows(writer: JsonlWriter, table: str) -> list[dict]:
    return writer.read_all(f"main.dbx_leaning.{table}")


async def test_a_run_with_no_app_completes_and_persists_everything(cfg, writer):
    """The single most important property: a job that starts while the app is
    down is a normal case, not an edge case."""
    conf = cfg(app_url=None, model_config={"steps": 4})
    harness = JobHarness(conf, writer=writer)

    outcome = await harness.run()

    assert outcome.status is RunStatus.SUCCEEDED
    assert outcome.observed_live is False
    assert outcome.write_failures == 0 and outcome.unflushed_rows == 0

    assert len(rows(writer, "run_progress")) == 4
    assert len(rows(writer, "results_fake")) == 4
    events = [r["status"] for r in rows(writer, "run_events")]
    assert events[0] == "RUNNING" and events[-1] == "SUCCEEDED"

    meta = rows(writer, "run_results_meta")
    assert len(meta) == 1 and meta[0]["row_count"] == 4
    assert json.loads(meta[0]["preview_json"])  # a preview was built


async def test_an_unreachable_app_is_not_an_error(cfg, writer):
    # Nothing is listening on this port; the run must be identical.
    conf = cfg(app_url="http://127.0.0.1:9", model_config={"steps": 2}, ws_reconnect_s=0.05)
    outcome = await JobHarness(conf, writer=writer).run()

    assert outcome.status is RunStatus.SUCCEEDED
    assert outcome.observed_live is False
    assert len(rows(writer, "results_fake")) == 2


async def test_seq_is_gap_free_across_the_whole_run(cfg, writer):
    conf = cfg(model_config={"steps": 3})
    outcome = await JobHarness(conf, writer=writer).run()

    seqs = sorted(
        r["seq"]
        for table in ("run_logs", "run_progress", "run_events", "run_results_meta")
        for r in rows(writer, table)
    )
    assert seqs == list(range(outcome.seq_issued))


async def test_cancellation_is_observed_within_one_poll_interval(cfg, writer):
    model = BlockingModel({"poll_s": 0.01, "timeout_s": 5.0})
    conf = cfg(model_spec="tests.job.conftest:BlockingModel")
    harness = JobHarness(conf, writer=writer, handle=describe_object(model, "blocking"))

    async def cancel_soon():
        await asyncio.to_thread(model.started.wait, 2.0)
        await asyncio.sleep(0.02)
        harness.token.cancel("cancelled by test")

    asyncio.create_task(cancel_soon())
    started = time.monotonic()
    outcome = await harness.run()

    assert outcome.status is RunStatus.CANCELLED
    assert outcome.detail == "cancelled by test"
    assert model.observed_at is not None
    assert model.observed_at - started < 1.0, "cancel took far longer than a poll interval"


async def test_results_are_written_even_when_the_run_is_cancelled(cfg, writer):
    """Results are not best-effort: a cancelled run keeps its incumbent."""
    model = BlockingModel({"poll_s": 0.01})
    conf = cfg(model_spec="tests.job.conftest:BlockingModel")
    harness = JobHarness(conf, writer=writer, handle=describe_object(model, "blocking"))

    async def cancel_soon():
        await asyncio.to_thread(model.started.wait, 2.0)
        harness.token.cancel("stop")

    asyncio.create_task(cancel_soon())
    outcome = await harness.run()

    assert outcome.status is RunStatus.CANCELLED
    assert outcome.result_rows == 1
    assert rows(writer, "results_blocking") == [
        {"run_id": "run-test", "chunk_index": 0, "partial": True}
    ]


async def test_a_model_that_raises_fails_the_run_without_taking_the_harness_down(cfg, writer):
    class Exploding:
        results_table = "results_boom"

        def run(self):
            raise ValueError("bad input")

    conf = cfg(model_spec="x:Exploding")
    outcome = await JobHarness(
        conf, writer=writer, handle=describe_object(Exploding(), "boom")
    ).run()

    assert outcome.status is RunStatus.FAILED
    assert "ValueError: bad input" in outcome.detail
    # The terminal status still reached the durable path.
    assert [r["status"] for r in rows(writer, "run_events")][-1] == "FAILED"


async def test_succeeded_is_impossible_over_a_lost_result_write(cfg, writer):
    """A run claiming success while its result write failed is worse than an
    honest failure — nobody goes looking for a problem that claims not to
    exist."""

    class DeadWriter:
        name = "dead"

        def write_batch(self, table, rows):
            raise RuntimeError("unity catalog unreachable")

        def close(self): ...

    conf = cfg(model_config={"steps": 2})
    outcome = await JobHarness(conf, writer=DeadWriter()).run()

    assert outcome.status is RunStatus.FAILED
    assert "Refusing to report SUCCEEDED" in outcome.detail
    assert outcome.unflushed_rows > 0


async def test_row_count_of_zero_is_reported_honestly(cfg, writer):
    class Empty:
        results_table = "results_empty"

        def run(self): ...
        def results(self):
            return []

    conf = cfg(model_spec="x:Empty")
    outcome = await JobHarness(conf, writer=writer, handle=describe_object(Empty(), "e")).run()

    assert outcome.status is RunStatus.SUCCEEDED
    meta = rows(writer, "run_results_meta")
    assert len(meta) == 1 and meta[0]["row_count"] == 0
    assert outcome.result_rows == 0


async def test_a_model_may_emit_results_in_chunks(cfg, writer):
    model = ChunkedModel({"chunks": 3, "per_chunk": 4})
    conf = cfg(model_spec="tests.job.conftest:ChunkedModel")
    outcome = await JobHarness(conf, writer=writer, handle=describe_object(model, "chunked")).run()

    assert outcome.status is RunStatus.SUCCEEDED
    assert outcome.result_chunks == 3, "each chunk must be its own result message"
    assert outcome.result_rows == 12

    meta = sorted(rows(writer, "run_results_meta"), key=lambda r: r["chunk_index"])
    assert [m["chunk_index"] for m in meta] == [0, 1, 2]
    assert [m["row_count"] for m in meta] == [4, 4, 4], "per-chunk count, not a running total"
    assert [m["final"] for m in meta] == [False, False, True]

    written = rows(writer, "results_chunked")
    assert len(written) == 12
    assert {r["chunk_index"] for r in written} == {0, 1, 2}


async def test_the_harness_does_not_double_write_a_streaming_models_results(cfg, writer):
    class ChunkedWithAccessor(ChunkedModel):
        def results(self):
            return [{"should": "not be written"}]

    model = ChunkedWithAccessor({"chunks": 2, "per_chunk": 1})
    conf = cfg(model_spec="x:ChunkedWithAccessor")
    outcome = await JobHarness(conf, writer=writer, handle=describe_object(model, "c")).run()

    assert outcome.result_chunks == 2
    assert all("should" not in r for r in rows(writer, "results_chunked"))


async def test_build_step_runs_before_the_model_does(cfg, writer):
    model = FakeModel({"steps": 1})
    conf = cfg(model_spec="tests.job.conftest:FakeModel")
    await JobHarness(conf, writer=writer, handle=describe_object(model, "fake")).run()
    assert model.built is True


async def test_results_table_resolution_order(cfg, writer):
    # config wins over the model's own attribute
    model = FakeModel({"steps": 1})
    conf = cfg(results_table="results_override")
    await JobHarness(conf, writer=writer, handle=describe_object(model, "fake")).run()
    assert len(rows(writer, "results_override")) == 1
    assert rows(writer, "results_fake") == []


async def test_a_model_with_no_results_table_gets_one_from_its_module_name(cfg, writer):
    class Bare:
        def run(self): ...
        def results(self):
            return [{"a": 1}]

    conf = cfg(model_spec="job.models.scenario", results_table=None)
    await JobHarness(conf, writer=writer, handle=describe_object(Bare(), "bare")).run()
    assert len(rows(writer, "results_scenario")) == 1


@pytest.mark.parametrize("steps", [1, 25])
async def test_the_durable_record_is_complete_at_any_size(cfg, writer, steps):
    conf = cfg(model_config={"steps": steps}, flush_max_bytes=200)
    outcome = await JobHarness(conf, writer=writer).run()
    assert outcome.unflushed_rows == 0
    assert len(rows(writer, "run_progress")) == steps
    assert len(rows(writer, "results_fake")) == steps
