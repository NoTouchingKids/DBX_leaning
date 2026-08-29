"""What the job knows about its own run: latest status, and a progress
history for the end-of-run summary.

The replay ring — answering a BACKFILL from memory — moved to
`job/stream.py`'s `RunStream`; see `test_stream.py` for that half. This file
is what is left: the two jobs `RunRecord`'s own docstring says `RunStream` has
no way to know.
"""

from __future__ import annotations

from job.record import RunRecord
from job.shared.envelope import RunStatus, make_message


def test_the_latest_status_replaces_itself_and_knows_when_it_is_terminal():
    record = RunRecord("r", model="scenario", job_run_id="jr-1")
    record.observe(make_message("status", run_id="r", seq=0, status="RUNNING"))
    assert record.status is RunStatus.RUNNING and record.terminal is False

    record.observe(
        make_message("status", run_id="r", seq=1, status="CANCELLED", detail="cancelled by kp")
    )
    assert record.status is RunStatus.CANCELLED and record.terminal is True

    summary = record.summary(requested_by="kp")
    assert summary["status"] == "CANCELLED"
    assert summary["detail"] == "cancelled by kp"
    assert summary["model"] == "scenario" and summary["job_run_id"] == "jr-1"
    assert summary["requested_by"] == "kp"


def test_latest_status_is_none_before_anything_arrives():
    record = RunRecord("r")
    assert record.status is None and record.latest_status is None and record.terminal is False


def test_progress_history_keeps_every_point_up_to_its_bound_and_the_latest_separately():
    record = RunRecord("r", progress_history=3)
    for i in range(5):
        record.observe(make_message("progress", run_id="r", seq=i, elapsed_seconds=float(i)))

    # Bounded, like the old ring — but this bound is NOT durability-gated:
    # nothing here depends on `note_flushed`, because nothing here answers a
    # BACKFILL. It exists only for `progress_rows()`/an end-of-run summary.
    assert [p.elapsed_seconds for p in record.progress_rows()] == [2.0, 3.0, 4.0]
    assert record.latest_progress.elapsed_seconds == 4.0


def test_counts_tracks_every_message_type_seen_regardless_of_progress_bound():
    record = RunRecord("r", progress_history=1)
    record.observe(make_message("log", run_id="r", seq=0, message="a"))
    record.observe(make_message("log", run_id="r", seq=1, message="b"))
    record.observe(make_message("progress", run_id="r", seq=2, elapsed_seconds=0.0))
    record.observe(make_message("status", run_id="r", seq=3, status="RUNNING"))

    assert record.counts() == {"log": 2, "progress": 1, "status": 1}


def test_a_run_that_recorded_no_status_summarises_as_failed():
    """The row still has to say something, and "nothing arrived" is not a
    success — a job that died before its terminal message is a failed run."""
    summary = RunRecord("r").summary()

    assert summary["status"] == "FAILED"
    assert "no terminal status" in summary["detail"]


def test_the_summary_carries_the_status_messages_own_seq_and_ts():
    """`run_status_history` dedupes on (run_id, seq) and orders by ts, so both
    have to be the *message's* — re-clocking a history row on the way out
    would make a redelivered report look like a second transition."""
    record = RunRecord("r", model="scenario")
    record.observe(make_message("status", run_id="r", seq=0, ts=1_000, status="RUNNING"))
    record.observe(make_message("status", run_id="r", seq=7, ts=1_700, status="SUCCEEDED"))

    summary = record.summary()

    assert summary["seq"] == 7 and summary["ts"] == 1_700
    assert summary["updated_ts"] > summary["ts"], "updated_ts is when it was reported"


def test_a_summary_with_no_status_message_has_no_seq_and_is_timestamped_anyway():
    """NULL seq is what keeps such a row outside the history table's partial
    unique index, and rightly: there is no message identity to dedupe it by.
    `ts` is NOT NULL there, so it falls back to when the report was made."""
    summary = RunRecord("r").summary()

    assert summary["seq"] is None
    assert summary["ts"] == summary["updated_ts"]


def test_the_summary_carries_every_column_its_two_tables_bind():
    """Positional binding in `LakebaseStatus._body()`: a key that quietly
    disappears here is a KeyError at report time, and a key that quietly
    appears is nothing until someone binds it. Both are worth failing on."""
    assert set(RunRecord("r").summary()) == {
        # the run_status columns, in both schemas
        "run_id",
        "job_run_id",
        "model",
        "status",
        "detail",
        "started_ts",
        "updated_ts",
        "requested_by",
        # the status message's own coordinates, for run_status_history
        "seq",
        "ts",
    }
