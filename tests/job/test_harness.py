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
from heartbeat import Heartbeat

from job.harness import Harness
from job.loader import describe_object
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
    assert outcome.live_offered == 0, "something was offered to a channel that is not there"
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
    assert outcome.live_offered == 0, "a raising channel was counted as a successful offer"
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


def test_offered_is_not_delivered(tmp_path):
    """A metric must not claim success over something that did not happen.

    The live channel queues and returns immediately — that is what keeps it off
    the model's thread — so every record is "offered" whether or not the app
    is reachable. A real deployed run on 2026-08-31 reported `observed=True`
    after six failed connection attempts, because the harness was counting
    queue puts and calling them sends.

    So the harness reports `live_offered` and nothing else; whether anything
    arrived is the channel's own count, and `job/main.py` logs that separately.
    """
    queued: list[dict] = []
    h = _harness(tmp_path, on_message=queued.append)  # a queue that never delivers
    outcome = h.run()

    assert outcome.live_offered == outcome.seq_issued
    assert not hasattr(outcome, "observed_live"), (
        "observed_live is back; the harness cannot know what a channel delivered"
    )


# --- the slots ---------------------------------------------------------------


def test_every_swappable_part_is_a_named_public_slot(tmp_path):
    """Writer, channel, token, seq, model handle and status writer are settable
    at construction and by plain assignment — no private attribute to poke."""
    from job.cancellation import CancellationToken
    from job.status import NullStatusWriter
    from shared.seq import SeqCounter

    token, seq = CancellationToken(), SeqCounter(start=10)
    h = Harness("r1", _writer(tmp_path), token=token, seq=seq)
    assert h.token is token and h.seq is seq
    assert h.channel is None and h.handle is None
    assert isinstance(h.status_writer, NullStatusWriter)

    seen: list[dict] = []
    h.channel = seen.append
    h.handle = describe_object(Heartbeat(seconds=0.05, hz=40), "heartbeat")
    outcome = h.run()

    assert outcome.status == "SUCCEEDED"
    assert seen and seen[0]["seq"] == 10, "the injected SeqCounter was not the one used"


def test_a_slot_cannot_be_replaced_once_the_run_has_started(tmp_path):
    h = _harness(tmp_path)
    h.run()
    with pytest.raises(RuntimeError, match="cannot be replaced"):
        h.channel = print


def test_channel_and_its_old_name_are_not_both_accepted(tmp_path):
    with pytest.raises(TypeError):
        Harness("r1", _writer(tmp_path), channel=print, on_message=print)


class _RecordingStatusWriter:
    def __init__(self, delay_s: float = 0.0, fail: bool = False) -> None:
        self.calls: list[tuple] = []
        self.threads: list[str] = []
        self.delay_s = delay_s
        self.fail = fail
        self.closed = False

    def write(self, run_id, seq, status, terminal, detail, ts):
        self.threads.append(threading.current_thread().name)
        if self.delay_s:
            threading.Event().wait(self.delay_s)
        self.calls.append((run_id, seq, status, terminal, detail, ts))
        if self.fail:
            raise RuntimeError("postgres is down")
        return True

    def close(self):
        self.closed = True


def test_the_status_writer_sees_every_status_with_its_own_seq_and_ts(tmp_path):
    sw = _RecordingStatusWriter()
    h = _harness(tmp_path, status_writer=sw)
    outcome = h.run()

    statuses = [r for r in _records(h.writer) if r["type"] == "status"]
    assert [c[1] for c in sw.calls] == [r["seq"] for r in statuses]
    assert sw.calls[0][2] == "RUNNING" and sw.calls[0][3] is False
    assert sw.calls[-1][:5] == ("r1", statuses[-1]["seq"], outcome.status, True, None)
    assert sw.calls[-1][5] == statuses[-1]["ts"]
    assert sw.closed, "the status writer was not closed at the end of the run"


def test_the_lakebase_writer_fits_the_slot():
    """Shape, not ancestry: `job/lakebase.py` is the real occupant."""
    from job.lakebase import LakebaseStatusWriter
    from job.status import StatusWriter

    assert issubclass(LakebaseStatusWriter, StatusWriter)


def test_a_raising_status_writer_does_not_fail_the_run(tmp_path):
    sw = _RecordingStatusWriter(fail=True)
    outcome = _harness(tmp_path, status_writer=sw).run()
    assert outcome.status == "SUCCEEDED"
    assert sw.calls, "the writer was never called"


# --- four threads: the controller, the status writer, the shutdown order -----


def test_the_status_writer_runs_on_the_controller_never_the_models_thread(tmp_path):
    sw = _RecordingStatusWriter()
    outcome = _harness(tmp_path, status_writer=sw).run()

    assert outcome.status_writes == len(sw.calls) >= 2
    assert set(sw.threads) == {"controller"}, sw.threads


def test_rolls_happen_on_the_controller(tmp_path):
    names: list[str] = []
    writer = PartFileWriter(tmp_path, "r1", max_age_s=0.0)
    original = writer.roll_if_due

    def watched():
        names.append(threading.current_thread().name)
        return original()

    writer.roll_if_due = watched  # type: ignore[method-assign]
    h = Harness(
        "r1",
        writer,
        handle=describe_object(Heartbeat(seconds=0.3, hz=20), "heartbeat"),
        roll_tick_s=0.01,
    )
    outcome = h.run()

    assert outcome.status == "SUCCEEDED"
    assert names and set(names) == {"controller"}


def test_a_slow_status_writer_stalls_neither_the_model_nor_the_run(tmp_path):
    """A Postgres write that hangs must cost the run at most its bounded
    shutdown wait, and the model nothing at all."""
    import time

    sw = _RecordingStatusWriter(delay_s=3.0)
    h = _harness(
        tmp_path, model=Heartbeat(seconds=0.2, hz=20), status_writer=sw, status_timeout_s=0.2
    )
    started = time.monotonic()
    outcome = h.run()
    elapsed = time.monotonic() - started

    assert outcome.status == "SUCCEEDED"
    assert elapsed < 2.0, f"the run waited on the status writer ({elapsed:.2f}s)"
    assert outcome.status_terminal_reported is False
    # And the durable record is whole regardless.
    assert _records(h.writer)[-1]["terminal"] is True


def test_a_raising_status_writer_is_counted_not_fatal(tmp_path):
    sw = _RecordingStatusWriter(fail=True)
    outcome = _harness(tmp_path, status_writer=sw).run()
    assert outcome.status == "SUCCEEDED"
    assert outcome.status_write_failures == len(sw.calls) >= 2
    assert outcome.status_writes == 0


def test_lakebase_is_never_told_a_status_the_volume_does_not_have(tmp_path):
    """At-most-stale, never ahead: at the moment the status writer is called,
    that status is already in a closed part file."""
    on_disk_at_write: list[bool] = []
    writer = PartFileWriter(tmp_path, "r1")  # default size/age: nothing rolls by itself

    class Checking(_RecordingStatusWriter):
        def write(self, run_id, seq, status, terminal, detail, ts):
            on_disk_at_write.append(seq in {r["seq"] for r in _records(writer)})
            return super().write(run_id, seq, status, terminal, detail, ts)

    h = Harness(
        "r1",
        writer,
        handle=describe_object(Heartbeat(seconds=0.1, hz=40), "heartbeat"),
        status_writer=Checking(),
        roll_tick_s=0.01,
    )
    outcome = h.run()

    assert outcome.status == "SUCCEEDED"
    assert on_disk_at_write and all(on_disk_at_write), on_disk_at_write
    assert outcome.status_writes == len(on_disk_at_write)


def test_a_status_whose_part_never_closes_is_withheld_from_lakebase(tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise OSError("volume unavailable")

    monkeypatch.setattr("pathlib.Path.write_text", boom)
    sw = _RecordingStatusWriter()
    outcome = _harness(tmp_path, status_writer=sw).run()

    assert outcome.status == "FAILED"
    assert sw.calls == [], "Lakebase was told about statuses the volume never got"
    assert outcome.status_writes_withheld >= 2


def test_the_shutdown_order_is_the_signed_off_one(tmp_path):
    """model result -> part files closed -> Lakebase terminal -> bye.

    A crash between any two of these must leave the part files as the floor
    and Lakebase at-most-stale, never the reverse — which only holds if they
    happen in this order.
    """
    order: list[str] = []
    lock = threading.Lock()

    def note(what: str) -> None:
        with lock:
            order.append(what)

    class ResultThenDone:
        def attach(self, emit, should_cancel):
            self.emit = emit

        def run(self):
            self.emit("result", row_count=3, fetch_hint={"table": "t"})

    class Channel:
        def send(self, record):
            pass

        def start(self, executor):
            note("channel-start")

        def close(self, timeout):
            note("bye")

    class Status(_RecordingStatusWriter):
        def write(self, run_id, seq, status, terminal, detail, ts):
            if terminal:
                note("lakebase-terminal")
            return super().write(run_id, seq, status, terminal, detail, ts)

        def close(self):
            note("lakebase-close")

    writer = PartFileWriter(tmp_path, "r1")
    append, close = writer.append, writer.close

    def watched_append(record):
        if record["type"] == "result":
            note("model-result")
        return append(record)

    def watched_close():
        note("parts-closed")
        return close()

    writer.append = watched_append  # type: ignore[method-assign]
    writer.close = watched_close  # type: ignore[method-assign]

    h = Harness(
        "r1",
        writer,
        handle=describe_object(ResultThenDone(), "result-then-done"),
        channel=Channel(),
        status_writer=Status(),
    )
    outcome = h.run()

    assert outcome.status == "SUCCEEDED"
    assert order == [
        "channel-start",
        "model-result",
        "parts-closed",
        "parts-closed",  # the second close is the terminal status itself landing
        "lakebase-terminal",
        "lakebase-close",
        "bye",
    ], order


def test_a_model_cannot_emit_its_own_terminal_status(tmp_path):
    """Exactly one terminal status per run, the harness's, the last record.
    A model that says `terminal=True` is recorded as non-terminal and warned —
    not failed, and its status name is kept."""

    class Eager:
        def attach(self, emit, should_cancel):
            self.emit = emit

        def run(self):
            self.emit("status", status="INFEASIBLE", terminal=True, detail="no solution")
            self.emit("status", status="INFEASIBLE", terminal=True)
            self.emit("log", message="still here")

    sw = _RecordingStatusWriter()
    h = _harness(tmp_path, model=Eager(), status_writer=sw)
    outcome = h.run()

    records = _records(h.writer)
    terminal = [r for r in records if r["type"] == "status" and r["terminal"]]
    assert len(terminal) == 1 and terminal[0] == records[-1]
    assert outcome.status == "SUCCEEDED"
    assert outcome.coerced_terminal == 2

    model_statuses = [r for r in records if r.get("status") == "INFEASIBLE"]
    assert len(model_statuses) == 2 and not any(r["terminal"] for r in model_statuses)
    warnings = [r for r in records if r["type"] == "log" and "only the harness" in r["message"]]
    assert len(warnings) == 1, "warn once per run, not once per offence"
    assert [c[3] for c in sw.calls].count(True) == 1
