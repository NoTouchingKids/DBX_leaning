"""A real model, through the real harness, into a real (local) durable store.

No Databricks, no network, no mocks of the pieces under test — only the writer
is swapped for local JSONL, which is what makes "the app is down and the job
runs anyway" a property this suite can actually assert.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from job.bus import WebSocketBus
from job.config import JobConfig
from job.delta import JsonlWriter
from job.record import RunRecord
from job.runner import JobHarness
from job.shared.envelope import MessageAdapter, RunStatus

# The harness's own socket fake, rather than a second one here: one place that
# knows what a websocket looks like to `job.bus`.
from tests.job.conftest import FakeSocket, connector, until


@pytest.fixture
def run_config(tmp_path):
    def _make(model_spec: str, **overrides):
        base = dict(
            run_id="e2e-1",
            model_spec=model_spec,
            writer="jsonl",
            local_root=str(tmp_path / "delta"),
            flush_tick_s=0.02,
            flush_max_age_s=0.05,
            app_url=None,
        )
        base.update(overrides)
        return JobConfig(**base)

    return _make


def table(writer: JsonlWriter, name: str) -> list[dict]:
    return writer.read_all(f"main.dbx_leaning.{name}")


async def test_the_scenario_model_runs_end_to_end_unobserved(run_config, tmp_path):
    writer = JsonlWriter(tmp_path / "delta")
    cfg = run_config("job.models.scenario", model_config={"progress_every": 20})

    outcome = await JobHarness(cfg, writer=writer).run()

    assert outcome.status is RunStatus.SUCCEEDED
    assert outcome.observed_live is False, "nothing was listening, and that is fine"
    assert outcome.unflushed_rows == 0 and outcome.write_failures == 0

    results = table(writer, "results_scenario")
    assert len(results) == 72 and outcome.result_rows == 72
    assert all(row["run_id"] == "e2e-1" for row in results)

    meta = table(writer, "run_results_meta")
    assert len(meta) == 1 and meta[0]["row_count"] == 72
    preview = json.loads(meta[0]["preview_json"])
    assert 0 < len(preview) <= 500, "the preview should be bounded and downsampled"

    events = [row["status"] for row in table(writer, "run_events")]
    assert events == ["RUNNING", "SUCCEEDED"]


async def test_everything_written_is_a_valid_envelope_message(run_config, tmp_path):
    """The durable record must re-validate against the same contract the wire
    uses — two shapes is how v1's reconnect bugs happened."""
    writer = JsonlWriter(tmp_path / "delta")
    await JobHarness(run_config("job.models.scenario"), writer=writer).run()

    rebuilt = []
    for name, type_ in (
        ("run_logs", "log"),
        ("run_progress", "progress"),
        ("run_events", "status"),
        ("run_results_meta", "result"),
    ):
        for row in table(writer, name):
            fields = {k: v for k, v in row.items() if not k.endswith("_json")}
            if type_ == "progress":
                fields["payload"] = json.loads(row["payload_json"])
            if type_ == "result":
                fields["preview"] = json.loads(row["preview_json"])
                fields["fetch_hint"] = json.loads(row["fetch_hint_json"])
            rebuilt.append(MessageAdapter.validate_python({"type": type_, **fields}))

    assert rebuilt
    seqs = sorted(m.seq for m in rebuilt)
    assert seqs == list(range(len(seqs))), "the durable record has gaps"


async def test_a_chunked_model_writes_each_chunk_as_it_goes(run_config, tmp_path):
    """`panel_fit` emits a result every `chunk_size` groups instead of once at
    the end. The harness has to number those emissions and count each one's
    rows on its own — a `row_count` that became a running total would make a
    partial run unreadable, and a chunk_index that repeated would make the
    results table unorderable."""
    writer = JsonlWriter(tmp_path / "delta")
    cfg = run_config("job.models.panel_fit", model_config={"chunk_size": 6})

    outcome = await JobHarness(cfg, writer=writer).run()

    assert outcome.status is RunStatus.SUCCEEDED
    assert outcome.result_chunks > 1
    meta = sorted(table(writer, "run_results_meta"), key=lambda r: r["chunk_index"])
    assert [m["chunk_index"] for m in meta] == list(range(len(meta)))
    assert [m["final"] for m in meta][:-1] == [False] * (len(meta) - 1)
    assert meta[-1]["final"] is True
    assert sum(m["row_count"] for m in meta) == len(table(writer, "results_panel_fit"))


async def test_a_run_cancelled_partway_keeps_what_it_produced(run_config, tmp_path):
    writer = JsonlWriter(tmp_path / "delta")
    # A panel big enough that the run lasts well beyond the cancel — the
    # default 48 groups fit in about 70ms, which would race the cancel rather
    # than test it.
    panel = [
        {"entity": f"g{g}", "code": f"C{g}", "year": float(1960 + i), "life_expectancy": 50.0 + i}
        for g in range(4000)
        for i in range(8)
    ]
    cfg = run_config(
        "job.models.panel_fit",
        model_config={
            "rows": panel,
            "limit": len(panel),
            "chunk_size": 20,
            "progress_every": 50,
        },
    )
    harness = JobHarness(cfg, writer=writer)

    async def cancel_once_something_exists():
        # Cancel on the first chunk rather than after a fixed sleep: the
        # property under test is "a cancelled run keeps what it produced",
        # which says nothing unless something was produced first. `until` is
        # bounded, so a model that never emits fails the assertions below
        # rather than hanging the suite.
        await until(lambda: harness.record.counts().get("result", 0) >= 1)
        harness.token.cancel("cancelled by test")

    asyncio.create_task(cancel_once_something_exists())
    outcome = await harness.run()

    assert outcome.status is RunStatus.CANCELLED
    assert outcome.result_chunks > 0, "a cancelled run kept nothing"
    assert outcome.result_rows > 0, "a cancelled run kept no rows"
    assert len(table(writer, "results_panel_fit")) == outcome.result_rows
    assert [r["status"] for r in table(writer, "run_events")][-1] == "CANCELLED"


@pytest.mark.slow
async def test_the_gurobi_model_runs_through_the_gurobi_driver(run_config, tmp_path):
    pytest.importorskip("gurobipy", reason="needs the [gurobi] extra")

    writer = JsonlWriter(tmp_path / "delta")
    cfg = run_config(
        "job.models.gurobi_scheduling",
        model_config={"staff_count": 12, "days": 7, "time_limit_s": 20},
    )

    outcome = await JobHarness(cfg, writer=writer).run()

    assert outcome.status is RunStatus.SUCCEEDED
    assert outcome.result_rows > 0

    # The harness captured the solver's log through the composed callback.
    solver_logs = [r for r in table(writer, "run_logs") if r["source"] == "gurobi"]
    assert solver_logs, "no Gurobi output was captured"
    assert all("\n" not in r["message"] for r in solver_logs), "chunks were not reassembled"

    for row in table(writer, "run_progress"):
        payload = json.loads(row["payload_json"])
        for value in (payload.get("incumbent"), payload.get("best_bound")):
            assert value is None or abs(value) < 1e100, "the ±1e100 sentinel leaked"


async def test_a_live_app_sees_the_same_run_the_durable_store_does(run_config, tmp_path):
    """The live path and the durable path must not diverge — one shape, one
    record, whichever way it travelled."""
    writer = JsonlWriter(tmp_path / "delta")
    # `FakeSocket.send` actually awaits. The fakes this replaced returned
    # without ever suspending, so the sender never yielded mid-batch and always
    # emptied before teardown — which is why a teardown that closed before it
    # drained, costing a fast run its entire live stream, sat here undetected.
    ws = FakeSocket(send_delay_s=0.001)

    cfg = run_config("job.models.scenario", model_config={"progress_every": 30})
    bus = WebSocketBus(
        "wss://test/ws/job/x",
        cfg.run_id,
        record=RunRecord(cfg.run_id),
        connect=connector(ws),
    )
    outcome = await JobHarness(cfg, writer=writer, bus=bus).run()
    seen = ws.messages()

    assert outcome.observed_live is True and outcome.live_undrained == 0

    # The direction the old assertion never checked: a run the app watched must
    # actually be told the run ended. This is what the teardown bug broke.
    terminal = [m for m in seen if m.type.value == "status" and m.status.value == "SUCCEEDED"]
    assert terminal, "the terminal status must reach a live observer, not just Delta"

    live_seqs = {m.seq for m in seen}
    durable_seqs = {
        row["seq"]
        for name in ("run_logs", "run_progress", "run_events", "run_results_meta")
        for row in table(writer, name)
    }
    # Everything seen live is in the durable record. (Not the reverse: the
    # live path may filter client_visible=False and drop under pressure.)
    assert live_seqs <= durable_seqs
    assert durable_seqs == set(range(outcome.seq_issued))
