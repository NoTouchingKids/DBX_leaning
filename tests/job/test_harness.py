"""The harness, on threads, with and without anything listening.

The tests worth reading are the autonomy ones. "The job is autonomous, the app
is an optional observer" is the invariant every other decision in this platform
follows from, and v3 designed for it without ever testing it adversarially —
so a run with no listener, and a run whose listener throws on every message,
are both asserted to be indistinguishable in the durable record.
"""

from __future__ import annotations

import json
import threading

import pytest

from job.harness import Harness
from job.loader import describe_object
from job.models.heartbeat import Heartbeat
from job.telemetry import PartFileWriter


def _writer(tmp_path, **kw):
    return PartFileWriter(tmp_path, "r1", max_bytes=kw.pop("max_bytes", 1), **kw)


def _harness(tmp_path, model=None, **kw):
    model = model or Heartbeat(seconds=0.05, hz=40)
    return Harness(
        "r1",
        _writer(tmp_path),
        handle=describe_object(model, "heartbeat"),
        roll_tick_s=0.01,
        **kw,
    )


def _records(writer: PartFileWriter) -> list[dict]:
    out = []
    for path in sorted(writer.run_dir.glob("part-*.jsonl")):
        with open(path, encoding="utf-8") as fh:
            out.extend(json.loads(line) for line in fh if line.strip())
    return out


def test_a_run_with_nothing_listening_is_fully_durable(tmp_path):
    """The normal case, not the degraded one — apps run ~8h/day, jobs do not.

    v3 designed for this and never tested it adversarially. If this fails, a
    3am scheduled run is producing nothing.
    """
    h = _harness(tmp_path)
    outcome = h.run()

    assert outcome.status == "SUCCEEDED"
    assert outcome.terminal is True
    assert outcome.observed_live is False
    assert outcome.unflushed == 0

    records = _records(h.writer)
    assert len(records) == outcome.seq_issued, "the durable record is missing messages"
    assert [r["seq"] for r in records] == list(range(len(records))), "seq is not gap-free"
    assert records[-1]["type"] == "status" and records[-1]["terminal"] is True


def test_a_listener_that_throws_on_every_message_changes_nothing_durable(tmp_path):
    """A live channel is best-effort by contract. A broken one must not be
    able to lose a record, delay one, or fail a run."""
    seen = []

    def hostile(record):
        seen.append(record)
        raise ConnectionResetError("the app went away")

    h = _harness(tmp_path, on_message=hostile)
    outcome = h.run()

    assert outcome.status == "SUCCEEDED"
    assert outcome.live_sent == 0, "a raising channel was counted as a successful send"
    assert len(_records(h.writer)) == outcome.seq_issued
    assert seen, "the channel was never even offered a message"


def test_the_durable_write_happens_before_the_live_send(tmp_path):
    """Order matters: the volume is the floor. A live channel that blocks must
    not be able to delay a record reaching the volume, so the send comes
    second and this pins it."""
    order: list[str] = []
    h = _harness(tmp_path, on_message=lambda _r: order.append("live"))
    original = h.writer.append

    def watched(record):
        order.append("durable")
        return original(record)

    h.writer.append = watched  # type: ignore[method-assign]
    h.run()

    assert order[:2] == ["durable", "live"]


def test_cancel_is_acknowledged_and_the_run_ends_cancelled(tmp_path):
    """The thing v3 could not do: it set a flag and replied nothing, so the
    app could not tell 'delivered' from 'lost'."""
    h = _harness(tmp_path, model=Heartbeat(seconds=30, hz=20))

    done = threading.Event()
    outcome_box: list = []

    def go():
        outcome_box.append(h.run())
        done.set()

    threading.Thread(target=go, daemon=True).start()
    # Let it get going, then cancel.
    for _ in range(200):
        if h.seq.issued > 2:
            break
        threading.Event().wait(0.01)

    ack = h.cancel(requested_by="kp")
    assert ack["accepted"] is True
    assert ack["already_cancelling"] is False
    assert ack["run_id"] == "r1"

    assert done.wait(10), "the run did not stop after a cancel"
    outcome = outcome_box[0]
    assert outcome.status == "CANCELLED"
    assert "kp" in (outcome.detail or "")


def test_a_second_cancel_says_it_was_already_cancelling(tmp_path):
    h = _harness(tmp_path)
    assert h.cancel()["already_cancelling"] is False
    assert h.cancel()["already_cancelling"] is True


def test_a_model_that_raises_fails_the_run_rather_than_the_process(tmp_path):
    class Exploding:
        def attach(self, emit, should_cancel):
            self.emit = emit

        def run(self):
            raise ValueError("bad input")

    h = _harness(tmp_path, model=Exploding())
    outcome = h.run()

    assert outcome.status == "FAILED"
    assert "ValueError" in (outcome.detail or "") and "bad input" in (outcome.detail or "")
    # And it is still durable: a failed run's telemetry is the most useful kind.
    records = _records(h.writer)
    assert records[-1]["status"] == "FAILED"
    assert any("model raised" in r.get("message", "") for r in records)


def test_a_run_never_reports_succeeded_over_a_lost_write(tmp_path, monkeypatch):
    """The rule the whole durability design exists to hold.

    A run claiming SUCCEEDED while its telemetry is gone is worse than an
    honest FAILED, because nobody goes looking for a problem that says it does
    not exist.
    """
    h = _harness(tmp_path)

    def boom(*_a, **_k):
        raise OSError("volume unavailable")

    monkeypatch.setattr("pathlib.Path.write_text", boom)
    outcome = h.run()

    assert outcome.status == "FAILED"
    assert "Refusing to report SUCCEEDED" in (outcome.detail or "")
    assert outcome.unflushed > 0


def test_a_model_may_name_its_own_status(tmp_path):
    """An open `status` field is what lets per-model categorical progress
    travel without the envelope needing a concept of it."""

    class Calibrating:
        def attach(self, emit, should_cancel):
            self.emit = emit

        def run(self):
            return "CALIBRATED"

    outcome = _harness(tmp_path, model=Calibrating()).run()
    assert outcome.status == "CALIBRATED"


def test_replay_serves_a_gap_from_the_runs_own_telemetry(tmp_path):
    h = _harness(tmp_path, model=Heartbeat(seconds=0.05, hz=40))
    h.run()

    everything = h.replay(0)
    assert [r["seq"] for r in everything] == list(range(h.seq.issued))

    window = h.replay(2, 4)
    assert [r["seq"] for r in window] == [2, 3, 4]


@pytest.mark.parametrize("hz", [10, 50])
def test_the_heartbeat_emits_progress_a_client_can_render_without_model_code(tmp_path, hz):
    """The envelope's own thesis: generic fields let ANY model render a useful
    progress view with zero model-specific frontend code. v3's frontend
    abandoned that at ~1,800 lines per model; v4 holds it as a rule."""
    h = _harness(tmp_path, model=Heartbeat(seconds=0.1, hz=hz))
    h.run()

    progress = [r for r in _records(h.writer) if r["type"] == "progress"]
    assert progress, "no progress messages at all"
    for record in progress:
        assert 0.0 <= record["percent_complete"] <= 100.0
        assert record["primary_metric_label"] == "ticks"
        assert record["elapsed_seconds"] >= 0.0
    assert progress[-1]["percent_complete"] == pytest.approx(100.0)
