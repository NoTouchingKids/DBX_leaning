"""What the job can answer about its own run, from memory.

`since()` is the whole point: a reconnecting browser tab asks the job, not the
SQL warehouse — whose cost is uptime. So the two bounds it reports have to be
exactly right, because the app decides "ask the job" or "ask SQL" from them
and has no other way to tell.
"""

from __future__ import annotations

from job.record import RunRecord
from job.shared.envelope import RunStatus, make_message


def log(seq: int, **fields):
    return make_message("log", run_id="r", seq=seq, message=f"l{seq}", **fields)


def filled(count: int, *, replay_messages: int = 2000) -> RunRecord:
    record = RunRecord("r", replay_messages=replay_messages)
    for seq in range(count):
        record.observe(log(seq))
    return record


def test_a_gap_the_ring_still_covers_comes_back_complete():
    record = filled(10)

    messages, complete = record.since(6)

    assert [m.seq for m in messages] == [7, 8, 9]
    assert complete is True, "the job held everything asked for"
    assert record.replay_from_seq == 0


def test_a_gap_reaching_below_the_ring_comes_back_incomplete():
    """The job served what it had; the rest is the warehouse's to answer. A
    partial answer presented as a whole one is how a client loses rows without
    ever seeing an error."""
    record = filled(10, replay_messages=4)

    messages, complete = record.since(2)

    assert [m.seq for m in messages] == [6, 7, 8, 9]
    assert complete is False
    assert record.replay_from_seq == 6, "the floor is what tells the app where to go instead"


def test_the_oldest_seq_still_held_is_itself_covered():
    # The boundary: asking for everything after seq 5 when 6 is the oldest
    # still held needs no warehouse.
    record = filled(10, replay_messages=4)
    assert record.since(5)[1] is True
    assert record.since(4)[1] is False


def test_limit_truncates_a_page_without_calling_it_incomplete():
    """A truncated page is still complete as far as it goes — the caller pages
    on by seq. Reporting False here would send every paging client to SQL."""
    record = filled(10)

    messages, complete = record.since(-1, limit=3)

    assert [m.seq for m in messages] == [0, 1, 2]
    assert complete is True


def test_an_empty_record_answers_completely_with_nothing():
    messages, complete = RunRecord("r").since(-1)
    assert messages == [] and complete is True


def test_client_invisible_logs_are_withheld_the_way_the_warehouse_withholds_them():
    """Two sources answering the same question differently is the failure the
    one-envelope design exists to prevent — the seq gap is honest."""
    record = RunRecord("r")
    record.observe(log(0))
    record.observe(log(1, client_visible=False))
    record.observe(log(2))

    messages, _ = record.since(-1)
    assert [m.seq for m in messages] == [0, 2]


def test_the_flush_mark_only_ever_moves_forwards():
    """Flushes are per-table and land out of order, so the honest answer to
    "what can the warehouse definitely serve" is the high-water mark."""
    record = RunRecord("r")
    assert record.flushed_through_seq == -1

    record.note_flushed(40)
    record.note_flushed(10)
    assert record.flushed_through_seq == 40


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
