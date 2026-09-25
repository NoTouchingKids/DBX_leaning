"""The controller thread: FIFO work, a timer, bounded waits, no raises."""

from __future__ import annotations

import threading

from job.controller import Controller


def test_work_runs_in_order_on_the_controller_thread():
    c = Controller(tick_s=60)
    c.start()
    seen: list[tuple[int, str]] = []
    try:
        for i in range(50):
            c.submit(lambda i=i: seen.append((i, threading.current_thread().name)))
        assert c.drain(5)
    finally:
        c.stop()
    assert [i for i, _ in seen] == list(range(50))
    assert {name for _, name in seen} == {"controller"}


def test_the_tick_fires_without_any_work_arriving():
    ticked = threading.Event()
    c = Controller(tick_s=0.01, on_tick=ticked.set)
    c.start()
    try:
        assert ticked.wait(5), "the timer never fired on an idle controller"
    finally:
        c.stop()


def test_a_raising_item_is_counted_and_the_thread_carries_on():
    c = Controller(tick_s=60)
    c.start()
    after = threading.Event()
    try:
        c.submit(lambda: 1 / 0)
        c.submit(after.set)
        assert after.wait(5)
    finally:
        c.stop()
    assert c.failures == 1


def test_drain_is_bounded_even_when_an_item_is_stuck():
    release = threading.Event()
    c = Controller(tick_s=60)
    c.start()
    try:
        c.submit(lambda: release.wait(10))
        assert c.drain(0.1) is False, "drain waited out a stuck item instead of giving up"
    finally:
        release.set()
        c.stop()


def test_submit_after_stop_is_refused_not_lost_silently():
    c = Controller(tick_s=60)
    c.start()
    c.stop()
    assert c.submit(lambda: None) is False
    assert c.drain(0.1) is False
