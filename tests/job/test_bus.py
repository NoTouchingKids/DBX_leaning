"""The one live path: what it says on connect, what it answers, what it drops.

The real question these cannot answer — whether the Databricks Apps ingress
passes an Upgrade and holds it — is what /spike-ws exists for. These cover
everything that is ours rather than the platform's.
"""

from __future__ import annotations

import asyncio

import pytest

from job.bus import DEFAULT_BACKFILL_LIMIT, WebSocketBus, _diagnosis
from job.record import RunRecord
from job.shared.envelope import make_message
from job.shared.protocol import ControlFrame, ControlKind, backfill, cancel, pack_frame, ping

from .conftest import FakeSocket, connector, until


def log(seq: int, **fields):
    return make_message("log", run_id="run-1", seq=seq, message=f"l{seq}", **fields)


def status(seq: int, value: str = "RUNNING"):
    return make_message("status", run_id="run-1", seq=seq, status=value)


@pytest.fixture
async def make_bus():
    """Builds buses and closes every one of them, pass or fail.

    Not tidiness. pytest-asyncio shuts the loop's async generators down before
    it cancels tasks, so a bus left running deadlocks the whole session on the
    socket's inbound iterator — a failing assertion would hang the suite
    instead of reporting itself.
    """
    made: list[WebSocketBus] = []

    def _make(ws: FakeSocket | None = None, record: RunRecord | None = None, **kw) -> WebSocketBus:
        kw.setdefault("connect", connector(ws) if ws is not None else None)
        bus = WebSocketBus(
            "ws://x/ws",
            "run-1",
            record=record if record is not None else RunRecord("run-1"),
            **kw,
        )
        made.append(bus)
        return bus

    yield _make

    for bus in made:
        await bus.close()


def refusing(url, **kwargs):
    raise ConnectionRefusedError("app is down")


# --- connecting ------------------------------------------------------------


async def test_hello_is_the_first_frame_and_carries_both_replay_bounds(make_bus):
    record = RunRecord("run-1", job_run_id="jr-9")
    record.observe(log(3990))
    record.note_flushed(3980)
    ws = FakeSocket()

    bus = make_bus(ws, record, next_seq=lambda: 4000)
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


async def test_a_refused_connection_retries_rather_than_giving_up(make_bus):
    """A run that starts while the app is down must attach by itself when the
    app comes back — no restart."""
    ws = FakeSocket()
    attempts = {"n": 0}

    def flaky(url, **kwargs):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionRefusedError("app is down")
        return ws

    bus = make_bus(reconnect_s=0.01, connect=flaky)
    await bus.start()
    assert await until(lambda: bus.is_connected)

    assert bus.connect_attempts >= 3 and bus.connects == 1


async def test_messages_offered_while_nothing_is_connected_are_held_not_dropped(make_bus):
    """The queue holds through a blip; the app catches up from hello plus
    BACKFILL. Giving up early would drop what a reconnect could have had."""
    bus = make_bus(reconnect_s=0.01, connect=refusing)
    await bus.start()
    for seq in range(3):
        bus.offer(log(seq))
    await asyncio.sleep(0.05)

    assert bus.pending == 3 and bus.dropped == 0 and bus.sent == 0


# --- inbound ---------------------------------------------------------------


async def test_an_inbound_cancel_reaches_the_handler(make_bus):
    ws = FakeSocket(inbound=[pack_frame(cancel("run-1", requested_by="kp"))])
    seen: list[str] = []

    bus = make_bus(ws, on_cancel=seen.append)
    await bus.start()
    assert await until(lambda: bool(seen))

    assert seen == ["kp"]


async def test_an_undecodable_inbound_frame_is_ignored_not_fatal(make_bus):
    ws = FakeSocket(
        inbound=[b"\xff\xfe not msgpack", pack_frame(cancel("run-1", requested_by="kp"))]
    )
    seen: list[str] = []

    bus = make_bus(ws, on_cancel=seen.append)
    await bus.start()
    # The frame after the junk still arrives: the reader survived it.
    assert await until(lambda: bool(seen))

    assert seen == ["kp"] and bus.is_connected


async def test_an_inbound_ping_is_answered_with_a_pong(make_bus):
    ws = FakeSocket(inbound=[pack_frame(ping("run-1"))])

    bus = make_bus(ws)
    await bus.start()
    assert await until(lambda: bool(ws.control(ControlKind.PONG)))


async def test_the_app_level_ping_goes_out_on_its_own(make_bus):
    """Not a WS protocol ping: a proxy can answer one of those without the
    frame ever reaching the app, which makes it useless for telling a dropped
    connection from a quiet one."""
    ws = FakeSocket()

    bus = make_bus(ws, ping_s=0.01)
    await bus.start()
    assert await until(lambda: bool(ws.control(ControlKind.PING)))


# --- backfill --------------------------------------------------------------


async def test_a_backfill_is_answered_from_the_replay_ring(make_bus):
    """The request that must never wake the SQL warehouse."""
    record = RunRecord("run-1")
    for seq in range(5):
        record.observe(log(seq))
    record.note_flushed(2)
    ws = FakeSocket()

    bus = make_bus(ws, record)
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


async def test_a_backfill_reaching_below_the_ring_says_so_rather_than_lying(make_bus):
    """complete=False is the app's signal to go to SQL for the rest. A partial
    answer presented as a whole one is how a client silently loses rows."""
    record = RunRecord("run-1", replay_messages=3)
    for seq in range(10):
        record.observe(log(seq))
    ws = FakeSocket()

    bus = make_bus(ws, record)
    await bus.start()
    assert await until(lambda: bus.is_connected)
    ws.push(pack_frame(backfill("run-1", after_seq=0)))
    assert await until(lambda: bool(ws.control(ControlKind.BACKFILL_RESULT)))

    payload = ws.control(ControlKind.BACKFILL_RESULT)[0].payload
    assert [m["seq"] for m in payload["messages"]] == [7, 8, 9]
    assert payload["complete"] is False
    assert payload["replay_from_seq"] == 7, "and this is where the app picks SQL up from"


async def test_one_backfill_reply_is_capped_however_much_is_asked_for(make_bus):
    """An unbounded reply would let one reconnect pack the whole ring into a
    single frame. A client with a bigger gap pages on by seq."""
    record = RunRecord("run-1")
    for seq in range(DEFAULT_BACKFILL_LIMIT + 100):
        record.observe(log(seq))
    ws = FakeSocket()

    bus = make_bus(ws, record)
    await bus.start()
    assert await until(lambda: bus.is_connected)
    ws.push(pack_frame(backfill("run-1", after_seq=-1, limit=10_000)))
    assert await until(lambda: bool(ws.control(ControlKind.BACKFILL_RESULT)))

    payload = ws.control(ControlKind.BACKFILL_RESULT)[0].payload
    assert len(payload["messages"]) == DEFAULT_BACKFILL_LIMIT
    assert payload["complete"] is True, "a truncated page is still complete as far as it goes"


# --- the queue -------------------------------------------------------------


async def test_a_full_queue_drops_the_oldest_and_counts_it(make_bus):
    """Dropping the oldest is safe here in a way it was not for the old relay:
    the durable copy exists and the replay ring still holds it, so a dropped
    message is recoverable by BACKFILL rather than gone."""
    bus = make_bus(queue_max=3)
    for seq in range(5):
        bus.offer(log(seq))

    assert bus.pending == 3 and bus.dropped == 2
    assert [m.seq for m in bus._q] == [2, 3, 4]


async def test_a_client_invisible_log_is_never_offered_live(make_bus):
    """Raw solver chatter is written durably and stays replayable; it just
    never travels to a browser."""
    bus = make_bus()
    bus.offer(log(0, client_visible=False))
    bus.offer(log(1))

    assert [m.seq for m in bus._q] == [1]
    assert bus.dropped == 0, "filtered is not dropped"


# --- teardown --------------------------------------------------------------


async def test_drain_lets_a_slow_socket_finish_before_the_close(make_bus):
    ws = FakeSocket(send_delay_s=0.002)
    bus = make_bus(ws, batch_max=10)
    await bus.start()
    assert await until(lambda: bus.is_connected)
    for seq in range(50):
        bus.offer(log(seq))

    left = await bus.drain(5.0)

    assert left == 0 and bus.pending == 0
    assert len(ws.messages()) == 50


async def test_drain_gives_up_at_its_deadline_rather_than_holding_the_run_open(make_bus):
    """Anything still unsent is durable and BACKFILL-able, so the bound costs
    latency, not data."""
    bus = make_bus(reconnect_s=0.01, connect=refusing)
    await bus.start()
    for seq in range(3):
        bus.offer(log(seq))

    assert await bus.drain(0.05) == 3
    assert bus.dropped == 0


async def test_a_deadline_the_backlog_outlives_still_costs_no_outcome(make_bus):
    """Order stays FIFO right through the drain. Hoisting the outcome to the
    front instead was tried and is wrong: a client that treats a terminal
    status as end-of-stream — the browser does — stops reading there and
    truncates the run. So the outcome still goes last, but the deadline is no
    longer allowed to eat it: it is sent directly, as the final frame, and the
    seq gap in front of it is the client's cue to BACKFILL.
    """
    ws = FakeSocket(send_delay_s=0.01)
    bus = make_bus(ws, batch_max=10)
    await bus.start()
    assert await until(lambda: bus.is_connected)
    for seq in range(100):
        bus.offer(log(seq))
    bus.offer(status(100, "SUCCEEDED"))

    left = await bus.drain(0.15)

    assert left > 0, "the deadline was meant to expire with a backlog behind it"
    seen = ws.messages()
    assert seen[-1].seq == 100, "the outcome must be the last frame, not the first"
    assert [m.seq for m in seen[:-1]] == list(range(len(seen) - 1)), "FIFO held ahead of it"


async def test_a_send_failure_ends_the_session_so_the_bus_can_redial(make_bus):
    """A half-open socket is the failure this guards.

    Marking the connection down is not enough on its own. ``_maintain``
    redials only when ``_session`` returns, and ``_session`` is parked on the
    INBOUND iterator — which a socket whose write direction has failed can
    keep open indefinitely. Without the close, the bus would sit there holding
    queued messages against a connection nothing would ever re-establish.
    """
    ws = FakeSocket()
    bus = make_bus(ws, reconnect_s=0.02)
    await bus.start()
    assert await until(lambda: bus.is_connected)

    ws.fail_on_send = True
    bus.offer(log(0))
    assert await until(lambda: not bus.is_connected)

    # Counted apart from a queue-full eviction: backpressure working as
    # designed and a connection failing are different facts about a run.
    assert bus.send_failures == 1
    assert bus.dropped == 0
    assert bus.sent == 0

    assert await until(lambda: ws.closed), "the session must be closed, not just marked down"
    assert await until(lambda: ws.opened >= 2), "and the reconnect loop must then redial"


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
