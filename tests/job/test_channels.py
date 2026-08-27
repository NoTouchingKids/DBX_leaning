"""WebSocket channel behaviour, against an injected fake connection.

The real question these cannot answer — whether the Databricks Apps ingress
passes an Upgrade and holds it — is what /spike-ws exists for. These cover
everything that is ours rather than the platform's.
"""

from __future__ import annotations

import asyncio

from job.channels import HttpPushChannel, WebSocketChannel
from job.shared.envelope import make_message
from job.shared.protocol import ControlFrame, ControlKind, cancel, pack_frame, unpack_frame


class FakeWS:
    def __init__(self, inbound=None, fail_on_send=False):
        self.sent: list[bytes] = []
        self.inbound = list(inbound or [])
        self.fail_on_send = fail_on_send
        self.closed = False
        self._released = asyncio.Event()

    async def send(self, data):
        if self.fail_on_send:
            raise ConnectionError("socket gone")
        self.sent.append(data)

    async def close(self):
        self.closed = True
        self._released.set()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for frame in self.inbound:
            yield frame
        await self._released.wait()  # stay "open" until closed


def connector(ws):
    def _connect(url, **kwargs):
        return ws

    return _connect


async def test_hello_is_the_first_frame_and_carries_the_resume_point():
    ws = FakeWS()
    ch = WebSocketChannel("ws://x/ws", "run-1", next_seq=lambda: 4000, connect=connector(ws))
    await ch.start()
    await asyncio.sleep(0.05)

    frame = unpack_frame(ws.sent[0])
    assert isinstance(frame, ControlFrame) and frame.kind is ControlKind.HELLO
    assert frame.payload["next_seq"] == 4000
    await ch.close()


async def test_an_inbound_cancel_reaches_the_handler():
    ws = FakeWS(inbound=[pack_frame(cancel("run-1", requested_by="kp"))])
    seen: list[ControlFrame] = []
    ch = WebSocketChannel("ws://x/ws", "run-1", on_control=seen.append, connect=connector(ws))
    await ch.start()
    await asyncio.sleep(0.05)

    assert [f.kind for f in seen] == [ControlKind.CANCEL]
    assert seen[0].payload["requested_by"] == "kp"
    await ch.close()


async def test_an_undecodable_inbound_frame_is_ignored_not_fatal():
    ws = FakeWS(inbound=[b"\xff\xfe not msgpack"])
    seen = []
    ch = WebSocketChannel("ws://x/ws", "run-1", on_control=seen.append, connect=connector(ws))
    await ch.start()
    await asyncio.sleep(0.05)
    assert seen == [] and ch.is_connected
    await ch.close()


async def test_a_send_failure_marks_the_channel_down_rather_than_raising():
    ws = FakeWS()
    ch = WebSocketChannel("ws://x/ws", "run-1", connect=connector(ws))
    await ch.start()
    await asyncio.sleep(0.05)
    assert ch.is_connected

    ws.fail_on_send = True
    assert await ch.send_many([make_message("log", run_id="r", seq=0, message="x")]) is False
    assert ch.is_connected is False
    await ch.close()


async def test_a_refused_connection_retries_rather_than_giving_up():
    """A run that starts while the app is down must attach by itself when the
    app comes back — no restart."""
    attempts = {"n": 0}
    ws = FakeWS()

    def flaky(url, **kwargs):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionRefusedError("app is down")
        return ws

    ch = WebSocketChannel("ws://x/ws", "run-1", reconnect_s=0.01, connect=flaky)
    await ch.start()
    for _ in range(50):
        await asyncio.sleep(0.01)
        if ch.is_connected:
            break

    assert attempts["n"] >= 3 and ch.is_connected
    await ch.close()


async def test_http_push_backs_off_after_a_failure():
    class Client:
        def __init__(self):
            self.calls = 0
            self.status = 500

        async def post(self, url, json=None, headers=None):
            self.calls += 1

            class R:
                status_code = self.status

            return R()

        async def aclose(self): ...

    client = Client()
    ch = HttpPushChannel("http://x/push", client=client, failure_backoff_s=60)
    assert ch.is_connected

    assert await ch.send_many([make_message("log", run_id="r", seq=0, message="x")]) is False
    assert ch.is_connected is False, "a failing app should not be hammered"
    assert ch.failures == 1


async def test_http_push_reports_success():
    class Client:
        async def post(self, url, json=None, headers=None):
            class R:
                status_code = 202

            return R()

        async def aclose(self): ...

    ch = HttpPushChannel("http://x/push", client=Client())
    assert await ch.send_many([make_message("log", run_id="r", seq=0, message="x")]) is True
    assert ch.posts == 1 and ch.is_connected
