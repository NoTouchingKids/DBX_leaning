"""The incremental-results case — the one model that emits results repeatedly.

These are also the tests that fail loudly if the harness stops supporting
multiple result emissions per run.

The model now backtests Databricks' sample hourly taxi volumes rather than a
generated sine wave. Nothing here touches a workspace: off-platform the shared
loader falls back to deterministic synthetic data, and these tests assert that
the fallback is *reported*, not hidden.
"""

from __future__ import annotations

import pytest

from job.models._data import nyc_taxi_hourly
from job.models.streaming_results import build_model

PROVENANCE_FIELDS = {
    "data_source",
    "data_synthetic",
    "data_rows",
    "data_fallback_reason",
}


# --- the incremental contract (must not regress) --------------------------


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


def test_a_cancelled_run_does_not_re_emit_or_roll_back_its_chunk(recorder):
    """Cancellation is the cleanest story here: nothing was being held, so
    there is nothing to finalise and nothing to withdraw."""
    r = recorder(cancel_on="result")
    model = r.attach(build_model())
    model.run()

    (chunk,) = r.of("result")
    origins = {row["origin"] for row in chunk["rows"]}
    assert origins == {model.origins[0]}, "the emitted chunk is the first window, unaltered"
    assert model.chunks_emitted == 1


def test_this_model_deliberately_exposes_no_results_accessor():
    """It has already streamed everything; a results() the harness also called
    would double-write."""
    from job.loader import describe_object

    handle = describe_object(build_model(), "job.models.streaming_results")
    assert handle.results is None
    assert handle.run is not None


def test_the_harness_still_supports_repeated_result_emissions():
    """The one gap this model exists to catch. If ``job/emitter.py`` stops
    giving each emission its own chunk_index and its own per-chunk row_count,
    fail here and loudly rather than in a deployed run's results table."""
    from job.emitter import Emitter

    emitted = []

    class _Sink:
        tables = type("T", (), {"qualify": staticmethod(lambda t: f"main.dbx.{t}")})()

        def append_message(self, msg):
            emitted.append(msg)

        def append_rows(self, table, rows):
            pass

    class _Relay:
        def offer(self, msg):
            pass

    emitter = Emitter("run-1", sink=_Sink(), relay=_Relay(), results_table="results_streaming")
    model = build_model({"n": 300, "step": 60})
    model.emit = emitter.emit
    model.should_cancel = lambda: False
    model.run()

    results = [m for m in emitted if m.type.value == "result"]
    assert len(results) > 1
    assert [m.chunk_index for m in results] == list(range(len(results))), (
        "the harness must give each emission its own chunk_index"
    )
    assert all(m.row_count == model.horizon for m in results), (
        "row_count must be this chunk's count, never a running total"
    )
    assert emitter.result_chunks == model.chunks_emitted
    assert emitter.result_rows_accepted == model.rows_emitted


# --- progress -------------------------------------------------------------


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


# --- the data, and saying where it came from ------------------------------


def test_it_backtests_the_sample_hourly_demand_series(recorder):
    """Same numbers the shared loader hands out — not a private sine wave."""
    r = recorder()
    model = r.attach(build_model({"days": 5}))
    model.build()

    assert model.series == nyc_taxi_hourly(days=5, seed=7).floats("trips")
    assert len(model.series) == 5 * 24


def test_it_runs_offline_on_the_fallback_without_a_workspace(recorder):
    """No Spark session here, so this exercises the fallback path end to end:
    a run must complete and stream chunks anyway."""
    r = recorder()
    model = r.attach(build_model())
    model.run()

    assert model.data is not None and model.data.synthetic is True
    assert model.chunks_emitted > 1
    assert r.of("result")[-1]["final"] is True
    r.validate_all()


def test_the_provenance_is_logged_at_the_input_phase(recorder):
    r = recorder()
    model = r.attach(build_model())
    model.run()

    input_logs = [line["message"] for line in r.of("log") if line["phase"] == "input"]
    assert any(model.data.provenance in message for message in input_logs), input_logs
    assert any("synthetic" in message for message in input_logs), (
        "a run that fell back must say so, not look like a run on real trips"
    )


def test_every_result_row_carries_the_provenance(recorder):
    """A run on real trips and a run that fell back must be distinguishable
    from the results table alone, long afterwards."""
    r = recorder()
    model = r.attach(build_model())
    model.run()

    described = model.data.describe()
    for chunk in r.of("result"):
        for row in chunk["rows"]:
            assert PROVENANCE_FIELDS <= set(row)
            assert row["data_source"] == described["data_source"]
            assert row["data_synthetic"] is True
            assert row["data_rows"] == described["data_rows"]
            assert row["data_fallback_reason"] == described["data_fallback_reason"]


def test_the_fallback_reason_column_exists_even_on_real_data(recorder):
    """Always present, so the results table's schema does not depend on
    whether a given run happened to fall back."""
    r = recorder()
    model = r.attach(build_model({"series": [float(i % 17) + i * 0.1 for i in range(400)]}))
    model.run()

    row = r.of("result")[0]["rows"][0]
    assert row["data_synthetic"] is False
    assert row["data_source"] == "config:series"
    assert row["data_fallback_reason"] is None


def test_a_caller_can_still_supply_its_own_series(recorder):
    r = recorder()
    series = [float(100 + i) for i in range(300)]
    model = r.attach(build_model({"series": series, "window": 100, "step": 50}))
    model.run()

    assert model.series == series
    assert model.data.synthetic is False
    # A straight line is trivially extrapolated; this is really asserting the
    # caller's own numbers were the ones backtested.
    assert r.of("progress")[-1]["primary_metric"] < 1.0


def test_a_series_too_short_to_backtest_still_ends_with_a_final_chunk(recorder):
    """A short real table must not produce a run with no final=true message —
    zero rows and 'never got that far' have to stay distinguishable."""
    r = recorder()
    model = r.attach(build_model({"series": [float(i) for i in range(50)]}))
    model.run()

    (chunk,) = r.of("result")
    assert chunk["rows"] == []
    assert chunk["final"] is True
    assert any(line["level"] == "WARNING" for line in r.of("log"))
    r.validate_all()
