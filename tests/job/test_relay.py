"""The live path is allowed to drop. The durable path is not — and by the time
anything reaches this queue, the durable copy already exists.
"""

from __future__ import annotations

import asyncio

from job.relay import LiveRelay
from job.shared.envelope import make_message


def log(seq: int, **kw):
    return make_message("log", run_id="r", seq=seq, message=f"l{seq}", **kw)


def progress(seq: int):
    return make_message("progress", run_id="r", seq=seq, elapsed_seconds=float(seq))


def status(seq: int):
    return make_message("status", run_id="r", seq=seq, status="RUNNING")


class Recorder:
    name = "recorder"

    def __init__(self, connected=True, accept=True):
        self._connected = connected
        self.accept = accept
        self.batches: list[list] = []

    @property
    def is_connected(self):
        return self._connected

    async def start(self): ...

    async def send_many(self, msgs):
        if not self.accept:
            return False
        self.batches.append(list(msgs))
        return True

    async def close(self): ...


def test_a_full_queue_drops_logs_first():
    relay = LiveRelay([], queue_max=3)
    for i in range(3):
        relay.offer(log(i))
    relay.offer(log(99))
    assert relay.pending == 3 and relay.dropped_logs == 1


def test_status_evicts_a_log_rather_than_being_dropped_itself():
    relay = LiveRelay([], queue_max=2)
    relay.offer(log(0))
    relay.offer(progress(1))
    relay.offer(status(2))

    kinds = [type(m).__name__ for m in relay._q]
    assert "LogMessage" not in kinds, "the log should have yielded"
    assert "StatusMessage" in kinds


def test_a_newer_progress_sample_supersedes_an_older_one():
    relay = LiveRelay([], queue_max=2)
    relay.offer(progress(0))
    relay.offer(progress(1))
    relay.offer(progress(2))
    assert [m.seq for m in relay._q] == [1, 2]


async def test_delivery_prefers_the_first_connected_channel():
    ws, http = Recorder(), Recorder()
    relay = LiveRelay([ws, http])
    relay.offer(log(0))
    pump = asyncio.create_task(relay.pump())
    await asyncio.sleep(0.05)
    await relay.stop()
    await asyncio.gather(pump, return_exceptions=True)

    assert ws.batches and not http.batches


async def test_falls_through_to_http_when_the_websocket_is_down():
    ws, http = Recorder(connected=False), Recorder()
    relay = LiveRelay([ws, http])
    relay.offer(log(0))
    pump = asyncio.create_task(relay.pump())
    await asyncio.sleep(0.05)
    await relay.stop()
    await asyncio.gather(pump, return_exceptions=True)

    assert not ws.batches and http.batches


async def test_falls_through_when_the_websocket_rejects_the_batch():
    ws, http = Recorder(accept=False), Recorder()
    relay = LiveRelay([ws, http])
    relay.offer(log(0))
    pump = asyncio.create_task(relay.pump())
    await asyncio.sleep(0.05)
    await relay.stop()
    await asyncio.gather(pump, return_exceptions=True)

    assert http.batches


async def test_a_channel_that_raises_does_not_stop_the_pump():
    class Broken:
        name = "broken"
        is_connected = True

        async def start(self): ...
        async def send_many(self, msgs):
            raise RuntimeError("boom")

        async def close(self): ...

    good = Recorder()
    relay = LiveRelay([Broken(), good])
    relay.offer(log(0))
    pump = asyncio.create_task(relay.pump())
    await asyncio.sleep(0.05)
    await relay.stop()
    await asyncio.gather(pump, return_exceptions=True)

    assert good.batches


async def test_no_channels_at_all_is_silent_not_fatal():
    relay = LiveRelay([])
    for i in range(5):
        relay.offer(log(i))
    pump = asyncio.create_task(relay.pump())
    await asyncio.sleep(0.05)
    await relay.stop()
    await asyncio.gather(pump, return_exceptions=True)

    assert relay.sent == 0 and relay.dropped == 5
