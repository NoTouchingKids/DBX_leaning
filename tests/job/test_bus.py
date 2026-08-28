"""The one live path: what it says on connect, what it answers, what it drops.

The real question these cannot answer — whether the Databricks Apps ingress
passes an Upgrade and holds it — is what /spike-ws exists for. These cover
everything that is ours rather than the platform's.
"""

from __future__ import annotations

import asyncio

from job.bus import DEFAULT_BACKFILL_LIMIT, WebSocketBus, _diagnosis
from job.record import RunRecord
from job.shared.envelope import make_message
from job.shared.protocol import ControlFrame, ControlKind, backfill, cancel, pack_frame, ping

from .conftest import FakeSocket, connector, until


def log(seq: int, **fields):
    return make_message("log", run_id="run-1", seq=seq, message=f"l{seq}", **fields)


def status(seq: int, value: str = "RUNNING"):
    return make_message("status", run_id="run-1", seq=seq, status=value)


def bus_for(ws: FakeSocket, record: RunRecord | None = None, **kw) -> WebSocketBus:
    return WebSocketBus(
        "ws://x/ws",
        "run-1",
        record=record if record is not None else RunRecord("run-1"),
        connect=connector(ws),
        **kw,
    )


# --- connecting ------------------------------------------------------------


async def test_hello_is_the_first_frame_and_carries_both_replay_bounds():
    record = RunRecord("run-1", job_run_id="jr-9")
    record.observe(log(3990))
    record.note_flushed(3980)
    ws = FakeSocket()

    bus = bus_for(ws, record, next_seq=lambda: 4000)
    await bus.start()
    assert await until(lambda: bool(ws.sent))

    frame = ws.frames()[0]
    assert isinstance(frame, ControlFrame) and frame.kind is ControlKind.HELLO
    # A job that has run unobserved for an hour attaches at 4000, not 0.
    assert frame.payload["next_seq"] == 4000
    assert frame.payload["job_run_id"] == "jr-9"
    # Both bounds travel so the app can decide "ask the job" vs "ask SQL"
    # without either side agreeing a threshold in advance.
    assert frame.payload["replay_from_seq"] == 3990
    assert frame.payload["flushed_through_seq"] == 3980
    await bus.close()


async def test_a_refused_connection_retries_rather_than_giving_up():
    """A run that starts while the app is down must attach by itself when the
    app comes back — no restart."""
    ws = FakeSocket()
    attempts = {"n": 0}

    def flaky(url, **kwargs):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionRefusedError("app is down")
        return ws

    bus = WebSocketBus(
        "ws://x/ws", "run-1", record=RunRecord("run-1"), reconnect_s=0.01, connect=flaky
    )
    await bus.start()
    assert await until(lambda: bus.is_connected)

    assert bus.connect_attempts >= 3 and bus.connects == 1
    await bus.close()


async def test_messages_offered_while_nothing_is_connected_are_held_not_dropped():
    """The queue holds through a blip; the app catches up from hello +
    BACKFILL. Giving up early would drop what a reconnect could have had."""

    def never(url, **kwargs):
        raise ConnectionRefusedError("app is down")

    bus = WebSocketBus(
        "ws://x/ws", "run-1", record=RunRecord("run-1"), reconnect_s=0.01, connect=never
    )
    await bus.start()
    for seq in range(3):
        bus.offer(log(seq))
    await asyncio.sleep(0.05)

    assert bus.pending == 3 and bus.dropped == 0 and bus.sent == 0
    await bus.close()


# --- inbound ---------------------------------------------------------------


async def test_an_inbound_cancel_reaches_the_handler():
    ws = FakeSocket(inbound=[pack_frame(cancel("run-1", requested_by="kp"))])
    seen: list[str] = []

    bus = bus_for(ws, on_cancel=seen.append)
    await bus.start()
    assert await until(lambda: bool(seen))

    assert seen == ["kp"]
    await bus.close()


async def test_an_undecodable_inbound_frame_is_ignored_not_fatal():
    ws = FakeSocket(
        inbound=[b"\xff\xfe not msgpack", pack_frame(cancel("run-1", requested_by="kp"))]
    )
    seen: list[str] = []

    bus = bus_for(ws, on_cancel=seen.append)
    await bus.start()
    # The frame after the junk still arrives: the reader survived it.
    assert await until(lambda: bool(seen))

    assert seen == ["kp"] and bus.is_connected
    await bus.close()


async def test_an_inbound_ping_is_answered_with_a_pong():
    ws = FakeSocket(inbound=[pack_frame(ping("run-1"))])

    bus = bus_for(ws)
    await bus.start()
    assert await until(lambda: bool(ws.control(ControlKind.PONG)))
    await bus.close()


async def test_the_app_level_ping_goes_out_on_its_own():
    """Not a WS protocol ping: a proxy can answer one of those without the
    frame ever reaching the app, which makes it useless for telling a dropped
    connection from a quiet one."""
    ws = FakeSocket()

    bus = bus_for(ws, ping_s=0.01)
    await bus.start()
    assert await until(lambda: bool(ws.control(ControlKind.PING)))
    await bus.close()


# --- backfill --------------------------------------------------------------


async def test_a_backfill_is_answered_from_the_replay_ring():
    """The request that must never wake the SQL warehouse."""
    record = RunRecord("run-1")
    for seq in range(5):
        record.observe(log(seq))
    record.note_flushed(2)
    ws = FakeSocket()

    bus = bus_for(ws, record)
    await bus.start()
    assert await until(lambda: bus.is_connected)
    ws.push(pack_frame(backfill("run-1", after_seq=1)))
    assert await until(lambda: bool(ws.control(ControlKind.BACKFILL_RESULT)))

    payload = ws.control(ControlKind.BACKFILL_RESULT)[0].payload
    assert payload["after_seq"] == 1
    assert [m["seq"] for m in payload["messages"]] == [2, 3, 4]
    assert payload["complete"] is True
    assert payload["replay_from_seq"] == 0
    assert payload["flushed_through_seq"] == 2
    assert bus.backfills_served == 1
    await bus.close()


async def test_a_backfill_reaching_below_the_ring_says_so_rather_than_lying():
    """complete=False is the app's signal to go to SQL for the rest. Serving a
    partial answer as if it were whole is how a client silently loses rows."""
    record = RunRecord("run-1", replay_messages=3)
    for seq in range(10):
        record.observe(log(seq))
    ws = FakeSocket()

    bus = bus_for(ws, record)
    await bus.start()
    assert await until(lambda: bus.is_connected)
    ws.push(pack_frame(backfill("run-1", after_seq=0)))
    assert await until(lambda: bool(ws.control(ControlKind.BACKFILL_RESULT)))

    payload = ws.control(ControlKind.BACKFILL_RESULT)[0].payload
    assert [m["seq"] for m in payload["messages"]] == [7, 8, 9]
    assert payload["complete"] is False
    assert payload["replay_from_seq"] == 7
    await bus.close()


async def test_one_backfill_reply_is_capped_however_much_is_asked_for():
    """An unbounded reply would let one reconnect pack the whole ring into a
    single frame. A client with a bigger gap pages on by seq."""
    record = RunRecord("run-1")
    for seq in range(DEFAULT_BACKFILL_LIMIT + 100):
        record.observe(log(seq))
    ws = FakeSocket()

    bus = bus_for(ws, record)
    await bus.start()
    assert await until(lambda: bus.is_connected)
    ws.push(pack_frame(backfill("run-1", after_seq=-1, limit=10_000)))
    assert await until(lambda: bool(ws.control(ControlKind.BACKFILL_RESULT)))

    payload = ws.control(ControlKind.BACKFILL_RESULT)[0].payload
    assert len(payload["messages"]) == DEFAULT_BACKFILL_LIMIT
    assert payload["complete"] is True, "a truncated page is still complete as far as it goes"
    await bus.close()


# --- the queue -------------------------------------------------------------


async def test_a_full_queue_drops_the_oldest_and_counts_it():
    """Dropping the oldest is safe here in a way it was not for the old relay:
    the durable copy exists and the replay ring still holds it, so a dropped
    message is recoverable by BACKFILL rather than gone."""
    bus = bus_for(FakeSocket(), queue_max=3)
    for seq in range(5):
        bus.offer(log(seq))

    assert bus.pending == 3 and bus.dropped == 2
    assert [m.seq for m in bus._q] == [2, 3, 4]


async def test_a_client_invisible_log_is_never_offered_live():
    """Raw solver chatter is written durably and kept replayable; it just does
    not travel to a browser."""
    bus = bus_for(FakeSocket())
    bus.offer(log(0, client_visible=False))
    bus.offer(log(1))

    assert [m.seq for m in bus._q] == [1]
    assert bus.dropped == 0, "filtered is not dropped"


# --- teardown --------------------------------------------------------------


async def test_drain_lets_a_slow_socket_finish_before_the_close():
    ws = FakeSocket(send_delay_s=0.002)
    bus = bus_for(ws, batch_max=10)
    await bus.start()
    assert await until(lambda: bus.is_connected)
    for seq in range(50):
        bus.offer(log(seq))

    left = await bus.drain(5.0)

    assert left == 0 and bus.pending == 0
    assert len(ws.messages()) == 50
    await bus.close()


async def test_drain_gives_up_at_its_deadline_rather_than_holding_the_run_open():
    """Anything still unsent is durable and BACKFILL-able, so the bound costs
    latency, not data."""

    def never(url, **kwargs):
        raise ConnectionRefusedError("app is down")

    bus = WebSocketBus(
        "ws://x/ws", "run-1", record=RunRecord("run-1"), reconnect_s=0.01, connect=never
    )
    await bus.start()
    for seq in range(3):
        bus.offer(log(seq))

    assert await bus.drain(0.05) == 3
    assert bus.dropped == 0
    await bus.close()


async def test_the_outcome_jumps_the_queue_when_the_drain_cannot_clear_it():
    """FIFO is right while a run is live and exactly wrong at teardown: the
    terminal status is the last message emitted, so a bounded drain over a
    backlog would reach everything except the one message that matters."""
    ws = FakeSocket(send_delay_s=0.01)
    bus = bus_for(ws, batch_max=10)
    await bus.start()
    assert await until(lambda: bus.is_connected)
    for seq in range(100):
        bus.offer(log(seq))
    bus.offer(status(100, "SUCCEEDED"))

    left = await bus.drain(0.15)

    assert left > 0, "the deadline was meant to expire with a backlog behind it"
    outcomes = [m for m in ws.messages() if m.type.value == "status"]
    assert [m.seq for m in outcomes] == [100]
    await bus.close()


async def test_a_send_failure_drops_the_connection_rather_than_raising():
    ws = FakeSocket()
    bus = bus_for(ws)
    await bus.start()
    assert await until(lambda: bus.is_connected)

    ws.fail_on_send = True
    bus.offer(log(0))
    assert await until(lambda: not bus.is_connected)

    assert bus.dropped == 1 and bus.sent == 0
    await bus.close()


class TestTheRedirectThatDoesNotSayWhatItMeans:
    """An unauthenticated handshake does not come back 401.

    The Databricks Apps proxy answers it with a 302 to the OAuth login page,
    the websockets client follows the redirect, and the error is about the URL
    it landed on:

        ws session ended (https://.../oidc/oauth2/v2.0/authorize?...
        isn't a valid URI: scheme isn't ws or wss)

    Nothing in that names the cause.
    """

    def test_the_oauth_redirect_is_explained(self):
        exc = ValueError(
            "https://dbc-9f8a3f01-0b4c.cloud.databricks.com/oidc/oauth2/v2.0/authorize"
            "?client_id=a97fa469&redirect_uri=https%3A%2F%2Fdbx-leaning.aws.databricksapps.com"
            " isn't a valid URI: scheme isn't ws or wss"
        )
        said = _diagnosis(exc)

        assert "rejecting the handshake" in said
        assert "CAN_USE" in said

    def test_an_ordinary_failure_is_not_editorialised(self):
        assert _diagnosis(ConnectionRefusedError("connection refused")) == ""
