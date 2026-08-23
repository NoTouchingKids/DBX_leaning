"""The incremental-results case — the one model that emits results repeatedly.

These are also the tests that fail loudly if the harness stops supporting
multiple result emissions per run.
"""

from __future__ import annotations

import pytest

from models.streaming_results import build_model


def test_results_arrive_in_more_than_one_emission(recorder):
    r = recorder()
    model = r.attach(build_model())
    model.run()

    results = r.of("result")
    assert len(results) > 1, "the entire point of this model is incremental results"
    assert model.chunks_emitted == len(results)
    r.validate_all()


def test_each_chunk_carries_its_own_rows_not_a_running_total(recorder):
    r = recorder()
    model = r.attach(build_model({"horizon": 12}))
    model.run()

    counts = [len(res["rows"]) for res in r.of("result")]
    assert all(c == 12 for c in counts), counts
    assert sum(counts) == model.rows_emitted


def test_only_the_last_chunk_is_marked_final(recorder):
    r = recorder()
    r.attach(build_model()).run()
    flags = [res["final"] for res in r.of("result")]
    assert flags[-1] is True
    assert not any(flags[:-1])


def test_the_model_supplies_neither_chunk_index_nor_row_count(recorder):
    """Those are the harness's to assign — a model that guessed them would
    drift from what was actually written."""
    r = recorder()
    r.attach(build_model()).run()
    for res in r.of("result"):
        assert "chunk_index" not in res and "row_count" not in res


def test_cancelling_leaves_already_emitted_chunks_alone(recorder):
    r = recorder(cancel_on="result")  # cancel as soon as the first chunk lands
    model = r.attach(build_model())
    model.run()

    results = r.of("result")
    assert len(results) == 1, "no further chunks after cancellation"
    assert results[0]["final"] is False, "an interrupted run's chunk is not the final one"
    assert model.rows_emitted == len(results[0]["rows"])


def test_this_model_deliberately_exposes_no_results_accessor():
    """It has already streamed everything; a results() the harness also called
    would double-write."""
    from job.loader import describe_object

    handle = describe_object(build_model(), "models.streaming_results")
    assert handle.results is None
    assert handle.run is not None


def test_backtest_error_is_reported_per_window(recorder):
    r = recorder()
    r.attach(build_model()).run()
    progress = r.of("progress")
    assert all(p["primary_metric_label"] == "window_mae" for p in progress)
    assert all(p["primary_metric"] >= 0 for p in progress)
    assert progress[-1]["percent_complete"] == 100.0


def test_forecasts_line_up_with_actuals(recorder):
    r = recorder()
    model = r.attach(build_model({"n": 400, "horizon": 6}))
    model.run()

    rows = r.of("result")[0]["rows"]
    assert [row["step"] for row in rows] == list(range(6))
    # Fields are rounded independently, so allow for that rather than
    # pretending the arithmetic is exact.
    assert all(
        row["abs_error"] == pytest.approx(abs(row["predicted"] - row["actual"]), abs=1e-5)
        for row in rows
    )
