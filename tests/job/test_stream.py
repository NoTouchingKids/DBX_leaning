"""`RunStream` is the one place a run's messages live, and cursors are how
every consumer -- a live socket, a BACKFILL reply, the durable flusher --
reads from it. The eviction rule is the crux: nothing may be evicted before
the durable consumer has confirmed it, however small the replay window is
configured to be, and a durable consumer that never confirms anything must
never lose a message either -- it grows the stream instead.
"""

from __future__ import annotations

import threading

import pytest

from job.shared.envelope import make_message
from job.shared.seq import SeqCounter
from job.stream import DEFAULT_REPLAY_WINDOW, RunStream


def log(seq: int, *, run_id: str = "r", **fields):
    return make_message("log", run_id=run_id, seq=seq, message=f"m{seq}", **fields)


def filled(count: int, *, replay_window: int = DEFAULT_REPLAY_WINDOW) -> RunStream:
    stream = RunStream("r", replay_window=replay_window)
    for seq in range(count):
        stream.append(log(seq))
    return stream


# --- append ------------------------------------------------------------


def test_appended_messages_read_back_in_seq_order_from_the_beginning():
    stream = filled(5)
    messages, _ = stream.read(-1)
    assert [m.seq for m in messages] == [0, 1, 2, 3, 4]


def test_append_rejects_a_message_stamped_for_a_different_run():
    stream = RunStream("r")
    other = make_message("log", run_id="other-run", seq=0, message="x")
    with pytest.raises(ValueError, match="other-run"):
        stream.append(other)


def test_append_rejects_a_seq_that_has_already_been_stored():
    """A repeat means the SeqCounter contract broke upstream -- silently
    accepting a second message at the same seq would corrupt the one
    invariant everything else here (sorted search, dedupe against
    backfilled records) relies on."""
    stream = RunStream("r")
    stream.append(log(5))
    with pytest.raises(ValueError, match="already present"):
        stream.append(log(5))


def test_a_message_appended_out_of_seq_order_is_still_stored_in_sorted_position():
    """Deterministic version of the concurrency property below: nothing here
    is timing-dependent, so a failure means the insert-sort is wrong, not
    that a thread got unlucky. `append` must not assume its caller reaches
    it in seq order -- only that seq order is total and gap-free."""
    stream = RunStream("r")
    for seq in (5, 3, 8, 1, 0, 9, 2):
        stream.append(log(seq))

    messages, _ = stream.read(-1)
    assert [m.seq for m in messages] == [0, 1, 2, 3, 5, 8, 9]
    assert stream.head_seq == 9, "head tracks the highest seq seen, not the most recent insert"


def test_concurrent_appends_from_several_threads_lose_nothing_and_keep_seq_order():
    """`SeqCounter.next()` and `append()` are two separate calls, so a
    thread that wins the counter race can still lose the scheduler race that
    follows it -- another thread's higher seq can land here first. Losing
    that race must not mean losing the message, duplicating one, or leaving
    the stream out of order.
    """
    stream = RunStream("r")
    counter = SeqCounter()
    threads, per_thread = 8, 200

    def worker() -> None:
        for _ in range(per_thread):
            stream.append(log(counter.next()))

    pool = [threading.Thread(target=worker) for _ in range(threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()

    messages, complete = stream.read(-1)
    assert complete is True
    assert [m.seq for m in messages] == list(range(threads * per_thread)), (
        "seq order or completeness broke under concurrent append"
    )
    assert len(stream) == threads * per_thread


# --- cursors -------------------------------------------------------------


def test_two_cursors_from_the_same_stream_advance_independently():
    stream = filled(10)
    a = stream.cursor()
    b = stream.cursor()

    first = a.take(3)
    assert [m.seq for m in first] == [0, 1, 2]
    assert a.position == 2
    assert b.position == -1, "creating and reading from a must not move b"

    second = b.take(100)
    assert [m.seq for m in second] == list(range(10)), "b starts from its own beginning"


def test_a_slow_cursor_does_not_hold_back_a_fast_one():
    stream = RunStream("r")
    fast = stream.cursor()
    slow = stream.cursor()

    for seq in range(5):
        stream.append(log(seq))
    assert [m.seq for m in fast.take(100)] == [0, 1, 2, 3, 4]

    for seq in range(5, 10):
        stream.append(log(seq))
    assert [m.seq for m in fast.take(100)] == [5, 6, 7, 8, 9], (
        "the fast cursor kept consuming new appends; a slow one never reading did not slow it"
    )
    assert slow.position == -1, "slow is exactly where it started -- not evicted, not forced along"
    assert [m.seq for m in slow.take(100)] == list(range(10)), (
        "everything slow missed is still here"
    )


def test_take_returns_up_to_the_limit_and_advances_by_exactly_what_it_returned():
    stream = filled(10)
    cursor = stream.cursor()

    first = cursor.take(3)
    assert [m.seq for m in first] == [0, 1, 2]
    assert cursor.position == 2

    rest = cursor.take(100)
    assert [m.seq for m in rest] == [3, 4, 5, 6, 7, 8, 9]
    assert cursor.position == 9


def test_an_empty_take_leaves_the_position_untouched():
    stream = filled(3)
    cursor = stream.cursor()
    cursor.take(100)
    assert cursor.position == 2

    assert cursor.take(100) == []
    assert cursor.position == 2, "nothing new must not reset or otherwise move the position"


def test_cursor_take_does_not_withhold_client_invisible_logs():
    """The durable consumer's cursor is exactly the reader that must see
    these -- `client_visible=False` only ever meant "not sent live", and the
    write that keeps such a message at all has to see it to write it.
    Filtering is `read`'s job for the BACKFILL contract, not the generic
    cursor every consumer is built on."""
    stream = RunStream("r")
    stream.append(log(0))
    stream.append(log(1, client_visible=False))

    messages = stream.cursor().take(100)
    assert [m.seq for m in messages] == [0, 1]


def test_a_cursors_lag_counts_seq_distance_from_the_head_evicted_or_not():
    stream = filled(10, replay_window=3)
    cursor = stream.cursor()  # never advanced
    assert cursor.lag == 10, "10 messages exist after position -1"

    stream.note_flushed(9)  # confirms everything; eviction trims to the window
    assert len(stream) == 3, "the precondition: eviction actually ran"
    assert cursor.lag == 10, "lag counts distance from the head, not what happens to be retained"


def test_a_cursor_left_behind_by_eviction_resumes_from_whatever_survived_it():
    stream = filled(10, replay_window=3)
    cursor = stream.cursor()  # positioned at -1, never read

    stream.note_flushed(9)  # evicts seq 0..6, retaining 7, 8, 9
    resumed = cursor.take(100)

    assert [m.seq for m in resumed] == [7, 8, 9], "the gap is skipped, not blocked on or raised"
    assert cursor.position == 9


# --- read() / BACKFILL ----------------------------------------------------


def test_read_at_or_above_the_retained_floor_reports_complete():
    stream = filled(10, replay_window=4)
    stream.note_flushed(9)  # evict_below = min(9, 9-4) = 5; retains 6..9

    assert stream.read(5)[1] is True, "5 is one below the oldest retained seq -- still covered"
    assert stream.read(4)[1] is False


def test_read_below_the_retained_floor_reports_incomplete():
    stream = filled(10, replay_window=4)
    stream.note_flushed(9)

    messages, complete = stream.read(2)
    assert [m.seq for m in messages] == [6, 7, 8, 9]
    assert complete is False
    assert stream.replay_from_seq == 6, "the floor is what tells the caller where to go instead"


def test_read_limit_truncates_a_page_without_calling_it_incomplete():
    """A truncated page is still complete as far as it goes -- the caller
    pages on by seq. Reporting False here would send every paging caller to
    the durable store for no reason."""
    stream = filled(10)

    messages, complete = stream.read(-1, limit=3)
    assert [m.seq for m in messages] == [0, 1, 2]
    assert complete is True


def test_an_empty_stream_answers_completely_with_nothing():
    messages, complete = RunStream("r").read(-1)
    assert messages == [] and complete is True


def test_read_withholds_client_invisible_logs_the_way_the_durable_backfill_does():
    """Two sources answering the same question differently is the failure
    the one-envelope design exists to prevent -- the seq gap this leaves is
    honest: the message exists, durably, and was never going to show up on
    the live path either."""
    stream = RunStream("r")
    stream.append(log(0))
    stream.append(log(1, client_visible=False))
    stream.append(log(2))

    messages, _ = stream.read(-1)
    assert [m.seq for m in messages] == [0, 2]


# --- eviction: the crux -----------------------------------------------


def test_eviction_never_passes_the_durable_cursor_even_when_the_replay_window_says_it_could():
    """A window of 1 asks to keep almost nothing once everything is durable
    -- but the durable mark, not the window, is what actually governs when
    it is the smaller of the two. Only seq 0..3 are confirmed, so eviction
    may not touch anything past that no matter how aggressively small the
    window is configured."""
    stream = filled(10, replay_window=1)
    stream.note_flushed(3)

    assert len(stream) == 6, "seq 4..9 survive though the window alone would keep just 1"
    assert stream.replay_from_seq == 4


def test_a_stalled_durable_cursor_grows_the_stream_rather_than_losing_messages():
    """The documented trade: `flushed_through_seq` stuck at -1 pins
    `evict_below` at -1 forever, so nothing evicts however long the run
    goes on and however small the window is -- the same unbounded growth
    `buffer.py` already has today, kept observable rather than silent."""
    stream = RunStream("r", replay_window=5)
    for seq in range(500):
        stream.append(log(seq))

    assert len(stream) == 500, "nothing durable yet, so nothing may be evicted"
    assert stream.replay_from_seq == 0
    assert stream.durable_lag == 500


def test_replay_from_seq_tracks_eviction_as_the_durable_mark_advances():
    stream = filled(20, replay_window=5)
    stream.note_flushed(14)  # 0..14 confirmed; evict_below = min(14, 19-5) = 14

    assert len(stream) == 5, "seq 15..19 -- exactly the window, once durable has caught up to it"
    assert stream.replay_from_seq == 15


def test_the_durable_mark_only_ever_moves_forwards():
    """Flushes are per table and can complete out of order, so the honest
    answer to "what can the durable store definitely serve" is the
    high-water mark, never whatever was reported most recently."""
    stream = RunStream("r")
    assert stream.flushed_through_seq == -1

    stream.note_flushed(40)
    stream.note_flushed(10)
    assert stream.flushed_through_seq == 40


def test_replay_from_seq_is_zero_before_anything_has_been_appended():
    assert RunStream("r").replay_from_seq == 0


def test_durable_lag_reports_the_gap_between_the_head_and_the_durable_mark():
    stream = filled(10)
    assert stream.durable_lag == 10

    stream.note_flushed(9)
    assert stream.durable_lag == 0


def test_len_reports_how_many_messages_are_currently_retained():
    stream = filled(7)
    assert len(stream) == 7

    stream.note_flushed(6)  # default window (2000) is far larger than 7 -- nothing evicts
    assert len(stream) == 7


def test_describe_names_the_run_the_retained_count_and_the_durable_gap():
    stream = filled(5)
    text = stream.describe()
    assert "r" in text
    assert "5" in text
    assert "head at 4" in text
    assert "durable through -1" in text
