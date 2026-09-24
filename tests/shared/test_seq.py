import threading

import pytest

from shared.seq import SeqCounter


def test_values_are_monotonic_and_gap_free():
    c = SeqCounter()
    assert [c.next() for _ in range(5)] == [0, 1, 2, 3, 4]
    assert c.issued == 5


def test_can_resume_from_a_known_point():
    # A job reattaching after a reconnect keeps counting, it does not restart.
    c = SeqCounter(start=4000)
    assert c.next() == 4000


def test_negative_start_is_refused():
    with pytest.raises(ValueError):
        SeqCounter(start=-1)


def test_concurrent_callers_never_collide():
    # The model's callback runs on a worker thread while the harness stamps
    # status messages on the event loop. Both draw from one counter.
    c = SeqCounter()
    seen: list[int] = []
    lock = threading.Lock()

    def worker():
        local = [c.next() for _ in range(500)]
        with lock:
            seen.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(seen) == 4000
    assert sorted(seen) == list(range(4000))  # no duplicates, no gaps
