"""The job's RPC client, against a real WebSocket server.

A real server rather than a mock: the point of this file is the framing and the
request/response pairing, and a mock that returns what we told it to proves
nothing about either. `websockets.sync.server` makes this cheap.
"""

from __future__ import annotations

import json
import queue
import threading
import time

import pytest
from websockets.sync.client import connect as ws_connect
from websockets.sync.server import serve

from job.ws import Outbox, RpcClient, app_client, diagnose
from shared.rpc import (
    PROTOCOL_VERSION,
    ErrorCode,
    Method,
    RpcError,
    failure,
    parse,
    request,
    success,
)


class Server:
    """A server that records what it receives and can call the job back.

    One thread reads the socket — the handler — and `call()` waits on a queue
    for the reply it wants. Two threads calling `recv()` on one connection is
    a `ConcurrencyError` in `websockets.sync`, which this file found the
    honest way by doing it.
    """

    def __init__(self, hello_error: dict | None = None):
        #: What the stand-in says to `hello`. None = accept, as the real app
        #: does for a compatible job; an error object = refuse, then close,
        #: which is exactly what `app/server/routes/rpc.py` does.
        self.hello_error = hello_error
        self.connections = 0
        self.frames: list[dict] = []
        #: The RAW frames, kept alongside the parsed ones so a test can assert
        #: what KIND each was. `websockets` hands back `str` for a text frame
        #: and `bytes` for a binary one, and it accepts both — which is
        #: precisely why this had to be recorded rather than trusted. See
        #: `test_every_frame_the_job_sends_is_text`.
        self.raw_frames: list[str | bytes] = []
        self.ready = threading.Event()
        self._responses: queue.Queue[dict] = queue.Queue()
        self._conn = None
        self._server = None
        self._thread = None

    def _handler(self, ws):
        self._conn = ws
        self.connections += 1
        self.ready.set()
        try:
            for raw in ws:
                self.raw_frames.append(raw)
                frame = json.loads(raw)
                self.frames.append(frame)
                if frame.get("method") == Method.HELLO:
                    # The real app answers hello before anything else happens,
                    # and the job waits for that answer — so a stand-in that
                    # stayed silent would be more lenient than the real thing.
                    if self.hello_error is None:
                        ws.send(
                            success(
                                frame["id"],
                                {
                                    "observed": True,
                                    "run_id": frame["params"]["run_id"],
                                    "protocol_version": PROTOCOL_VERSION,
                                    "capabilities": {},
                                },
                            )
                        )
                    else:
                        ws.send(
                            failure(
                                frame["id"],
                                RpcError(
                                    self.hello_error["code"],
                                    self.hello_error["message"],
                                    self.hello_error.get("data"),
                                ),
                            )
                        )
                        ws.close(code=1008, reason="hello refused")
                        return
                    continue
                if "result" in frame or "error" in frame:
                    self._responses.put(frame)
        except Exception:  # noqa: BLE001 - client went away; that is the test ending
            pass

    def __enter__(self):
        self._server = serve(self._handler, "127.0.0.1", 0)
        self.port = self._server.socket.getsockname()[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        if self._server is not None:
            self._server.shutdown()

    def call(self, method, params, id):
        """Send a request to the job and wait for the reply with that id."""
        self._conn.send(request(method, params, id=id))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            frame = self._responses.get(timeout=5)
            if frame.get("id") == id:
                return parse(json.dumps(frame))
        raise AssertionError(f"no reply to id={id}")

    def methods(self):
        return [f.get("method") for f in self.frames if "method" in f]

    def wait_for(self, method, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if method in self.methods():
                return True
            time.sleep(0.02)
        return False


def _client(server, **kw):
    return RpcClient(
        f"ws://127.0.0.1:{server.port}",
        "r1",
        connect=lambda: ws_connect(f"ws://127.0.0.1:{server.port}"),
        on_cancel=kw.pop("on_cancel", lambda who: {"accepted": True, "by": who}),
        on_replay=kw.pop("on_replay", lambda a, b: [{"seq": a}]),
        **kw,
    )


def test_it_says_hello_with_the_seq_it_is_picking_up_from():
    """A job that has run unobserved for an hour attaches at seq 4,000, not 0.

    Saying so up front is what lets the app know it has a gap immediately,
    rather than inferring one from a jump it might equally read as a bug.
    """
    with Server() as server:
        client = _client(server, next_seq=lambda: 4000)
        client.start()
        try:
            assert server.wait_for(Method.HELLO)
            hello = next(f for f in server.frames if f.get("method") == Method.HELLO)
            assert hello["params"]["run_id"] == "r1"
            assert hello["params"]["next_seq"] == 4000
            assert "id" in hello, "hello is a request; the app is expected to answer it"
        finally:
            client.stop()


def test_hello_carries_the_protocol_version_and_capabilities():
    """LSP's `initialize`, in miniature: the version the app judges the job
    by, and the methods the job will answer."""
    with Server() as server:
        client = _client(server)
        client.start()
        try:
            assert server.wait_for(Method.HELLO)
            hello = next(f for f in server.frames if f.get("method") == Method.HELLO)
            assert hello["params"]["protocol_version"] == PROTOCOL_VERSION
            caps = hello["params"]["capabilities"]
            assert isinstance(caps, dict)
            assert {Method.CANCEL, Method.REPLAY, Method.PING} <= set(caps)
        finally:
            client.stop()


def test_a_refused_hello_means_unobserved_and_no_retry():
    """An incompatible version is not a dropped socket.

    Retrying it would get the same answer forever — ten reconnects per run,
    each one a warning in the app's log — so the client stops at the first
    refusal, records why, and the run carries on unobserved. Nothing here
    raises into the caller: `send()` keeps working and keeps being ignored.
    """
    error = {
        "code": ErrorCode.INCOMPATIBLE_VERSION,
        "message": "job protocol 2.0 is not compatible with app protocol 1.0",
        "data": {"app_protocol_version": "1.0"},
    }
    with Server(hello_error=error) as server:
        client = _client(server, backoff_s=0.01)
        client.start()
        try:
            assert server.wait_for(Method.HELLO)
            client._thread.join(timeout=5)
            assert not client._thread.is_alive(), "the socket thread kept retrying"

            client.send({"type": "log", "seq": 1})  # must not raise
            time.sleep(0.2)
        finally:
            client.stop()

        assert server.connections == 1, "a refused hello was retried"
        assert client.rejected is not None
        assert client.rejected["code"] == ErrorCode.INCOMPATIBLE_VERSION
        assert "hello refused" in (client.last_error or "")
        assert Method.TELEMETRY not in server.methods(), "streamed before hello was accepted"


def test_telemetry_waits_for_hello_to_be_accepted():
    """Records queued before the app answers are held, not sent into a
    handshake the app might be about to refuse."""
    with Server() as server:
        client = _client(server)
        for seq in range(3):
            client.send({"type": "log", "seq": seq})
        client.start()
        try:
            assert server.wait_for(Method.TELEMETRY)
        finally:
            client.stop()
        methods = server.methods()
        assert methods.index(Method.HELLO) < methods.index(Method.TELEMETRY)


def test_records_are_batched_into_telemetry_notifications():
    with Server() as server:
        client = _client(server, batch_max=100)
        client.start()
        try:
            assert server.wait_for(Method.HELLO)
            for seq in range(50):
                client.send({"type": "log", "seq": seq})
            assert server.wait_for(Method.TELEMETRY)

            time.sleep(0.3)
            batches = [f for f in server.frames if f.get("method") == Method.TELEMETRY]
            seqs = [m["seq"] for f in batches for m in f["params"]["messages"]]
            assert seqs == list(range(50))
            assert all("id" not in f for f in batches), "telemetry must be a notification"
        finally:
            client.stop()


def test_cancel_is_answered(monkeypatch):
    """The acknowledgement v3 could not give."""
    with Server() as server:
        seen = []
        client = _client(server, on_cancel=lambda who: seen.append(who) or {"accepted": True})
        client.start()
        try:
            assert server.wait_for(Method.HELLO)
            reply = server.call(Method.CANCEL, {"requested_by": "kp"}, id=99)
            assert reply.id == 99
            assert reply.ok and reply.result == {"accepted": True}
            assert seen == ["kp"]
        finally:
            client.stop()


def test_replay_returns_the_records_the_job_still_has():
    with Server() as server:
        client = _client(
            server, on_replay=lambda a, b: [{"seq": s} for s in range(a, (b or a) + 1)]
        )
        client.start()
        try:
            assert server.wait_for(Method.HELLO)
            reply = server.call(Method.REPLAY, {"from_seq": 3, "to_seq": 6}, id=1)
            assert reply.ok
            assert reply.result["count"] == 4
            assert [m["seq"] for m in reply.result["messages"]] == [3, 4, 5, 6]
        finally:
            client.stop()


def test_replay_with_bad_params_is_an_error_object_not_a_dropped_frame():
    with Server() as server:
        client = _client(server)
        client.start()
        try:
            assert server.wait_for(Method.HELLO)
            reply = server.call(Method.REPLAY, {"from_seq": "three"}, id=2)
            assert not reply.ok
            assert reply.error["code"] == ErrorCode.INVALID_PARAMS
        finally:
            client.stop()


def test_an_unknown_method_is_refused_by_name():
    with Server() as server:
        client = _client(server)
        client.start()
        try:
            assert server.wait_for(Method.HELLO)
            reply = server.call("summon", {}, id=3)
            assert not reply.ok
            assert reply.error["code"] == ErrorCode.METHOD_NOT_FOUND
            assert "summon" in reply.error["message"]
        finally:
            client.stop()


def test_ping_is_answered_at_the_application_level():
    """Not a WebSocket protocol ping: a proxy can answer those without the
    handler ever seeing them, which makes them useless for telling 'the
    ingress dropped this' from 'nothing was sent for a while'."""
    with Server() as server:
        client = _client(server)
        client.start()
        try:
            assert server.wait_for(Method.HELLO)
            reply = server.call(Method.PING, {}, id=4)
            assert reply.ok and reply.result["pong"] is True
        finally:
            client.stop()


def test_a_clean_stop_says_bye():
    """So the app can tell 'the run finished' from 'the socket dropped'."""
    with Server() as server:
        client = _client(server)
        client.start()
        assert server.wait_for(Method.HELLO)
        client.stop()
        assert server.wait_for(Method.BYE, timeout=3)


def test_send_never_blocks_and_drops_the_oldest_when_full():
    """The model thread calls this. It must return immediately whatever the
    channel is doing, and a full queue drops old records rather than refusing
    new ones — if the channel is behind, recent telemetry is what a watching
    human wants, and nothing is lost that matters because the volume has it."""
    client = RpcClient(
        "ws://127.0.0.1:1",
        "r1",
        connect=lambda: (_ for _ in ()).throw(OSError("nothing here")),
        on_cancel=lambda who: {},
        on_replay=lambda a, b: [],
        queue_max=10,
    )

    started = time.monotonic()
    for seq in range(1000):
        client.send({"seq": seq})
    assert time.monotonic() - started < 1.0, "send blocked the caller"
    assert client.dropped > 0

    drained = [r["seq"] for r in client.outbox.pending()]
    assert drained[-1] == 999, "the newest record was dropped instead of the oldest"
    assert drained == list(range(990, 1000))


def test_an_unreachable_app_gives_up_without_touching_the_run():
    """A run with no live channel is Tuesday, not a failure."""
    client = RpcClient(
        "ws://127.0.0.1:1",
        "r1",
        connect=lambda: (_ for _ in ()).throw(ConnectionRefusedError("no app")),
        on_cancel=lambda who: {},
        on_replay=lambda a, b: [],
        max_failures=3,
        backoff_s=0.01,
    )
    client.start()
    client.stop(timeout=5)

    assert client.connects == 0
    assert "ConnectionRefusedError" in (client.last_error or "")


@pytest.mark.parametrize(
    "text",
    [
        # What a REAL deployed run produced, 2026-08-31. `websockets.sync`
        # refuses to follow the redirect and reports only the status.
        "server rejected WebSocket connection: HTTP 302",
        # What v3 saw, where the client followed it and complained about the
        # scheme of the page it landed on.
        "https://x.cloud.databricks.com/oidc/oauth2/v2.0/authorize?client_id=a"
        " isn't a valid URI: scheme isn't ws or wss",
    ],
)
def test_an_ingress_redirect_is_explained_rather_than_relayed(text):
    """Both forms of the same fault, and neither names it.

    The Apps proxy answers an unauthenticated upgrade with a 302 to the OAuth
    login page — never a 401 — so "no Databricks identity" and "principal
    lacks CAN_USE" both surface as a redirect. This function existed before
    the first deploy and matched only the v3 wording, so it stayed silent
    through six real attempts. That is why the bare status is tested first.
    """
    said = diagnose(ValueError(text))
    assert "CAN_USE" in said
    assert "unobserved" in said


def test_a_refusal_is_distinguished_from_a_redirect():
    """The proxy redirects rather than refusing, so a 401/403 came from the app
    itself — which now authenticates nothing, so it is not a credential."""
    said = diagnose(ValueError("server rejected WebSocket connection: HTTP 403"))
    assert said
    assert "CAN_USE" not in said, "that is the 302, and sending someone there wastes the trip"
    assert "credential" in said


def test_an_ordinary_failure_is_not_editorialised():
    assert diagnose(ConnectionRefusedError("connection refused")) == ""


def test_a_503_is_explained_as_an_empty_ingress_not_an_auth_fault():
    """A real run logged nine of these and got no explanation at all.

    The proxy is up and the app is not behind it: compute stopped, or no
    active deployment. Reading it as an auth problem sends you to the grant,
    which is where the previous failure lived and is exactly the wrong place —
    a 503 never reached the app's own token check.
    """
    said = diagnose(ValueError("server rejected WebSocket connection: HTTP 503"))

    assert said, "a 503 must not be silent; that is what this function is for"
    assert "nothing behind it" in said
    assert "apps start" in said and "bundle run" in said
    assert "CAN_USE" not in said, "a 503 is not the grant problem"


# --- app_client's identity wiring -------------------------------------------


def test_app_client_builds_one_m2m_provider_for_the_whole_run(monkeypatch):
    """The reason `app_client` constructs the provider itself rather than
    leaving it to `headers()`: one provider's cache spans every reconnect in
    the run. Built inside the closure, a reconnect would rebuild it — and
    rebuild its cache empty — on every single attempt.
    """
    built: list[tuple] = []

    class RecordingM2M:
        def __init__(self, host, client_id, client_secret):
            built.append((host, client_id, client_secret))

        def token(self):
            return "tok"

    monkeypatch.setattr("job.auth.M2MTokenProvider", RecordingM2M)
    monkeypatch.setattr("websockets.sync.client.connect", lambda *a, **k: None)

    client = app_client(
        "https://app.example.com",
        "run-1",
        on_cancel=lambda who: {},
        on_replay=lambda a, b: [],
        workspace_host="https://ws.example.com",
        client_id="sp-1",
        client_secret="shh",
    )

    # Three "connection attempts" — three calls to the connect callable the
    # harness would actually make on reconnect.
    for _ in range(3):
        client._connect()

    assert len(built) == 1, f"the provider was rebuilt {len(built)} times, not reused"
    assert built[0] == ("https://ws.example.com", "sp-1", "shh")


def test_app_client_builds_no_provider_without_client_credentials():
    """The default path — the job's own runtime identity — needs no M2M
    exchange at all, so nothing should be constructed for it."""
    client = app_client(
        "https://app.example.com",
        "run-1",
        on_cancel=lambda who: {},
        on_replay=lambda a, b: [],
        workspace_host="https://ws.example.com",
    )
    # No assertion beyond "this does not raise while building the closure" —
    # there is no provider to inspect, which is the point. The real check is
    # in job/auth.py: `auth_headers` with no client_id/secret never touches
    # `m2m` at all.
    assert client is not None


def test_every_frame_the_job_sends_is_text():
    """The bug this file did not catch, now pinned.

    `shared.rpc`'s builders returned `bytes`, so `ws.send(...)` in `job/ws.py`
    emitted BINARY frames. The app reads with starlette's `receive_text()`,
    which wants `message["text"]` and gets `message["bytes"]`:

        raw = await websocket.receive_text()
        KeyError: 'text'

    Every frame the job sent was affected — hello first, so the socket died on
    the app's first read of every run.

    Nothing here caught it because `websockets.sync.server` accepts either
    kind and `json.loads` takes either, so the assertions above all passed on
    binary frames. The app's own tests did not catch it either, for the mirror
    reason: they build their own frames with `send_text`, so they never
    exercised what the job actually puts on the wire. Two lenient stand-ins,
    one real incompatibility between them.

    `websockets` returns `str` for a text frame and `bytes` for a binary one,
    which makes the frame KIND directly assertable.
    """
    with Server() as server:
        client = _client(server, batch_max=10)
        client.start()
        try:
            assert server.wait_for(Method.HELLO)
            client.send({"type": "log", "seq": 1})
            assert server.wait_for(Method.TELEMETRY)
        finally:
            client.stop()

        assert server.raw_frames, "nothing arrived; the test proves nothing"
        binary = [f for f in server.raw_frames if not isinstance(f, str)]
        assert not binary, (
            f"{len(binary)} of {len(server.raw_frames)} frames were binary. "
            f"Starlette's receive_text() raises KeyError: 'text' on those, so "
            f"the app drops the socket on its first read."
        )


# --- the drop policy (signed off 2026-09-24) ---------------------------------


def _rec(type_: str, seq: int) -> dict:
    return {"type": type_, "seq": seq}


def test_a_full_outbox_drops_logs_first():
    box = Outbox(live_max=4)
    box.put(_rec("log", 0))
    box.put(_rec("progress", 1))
    box.put(_rec("log", 2))
    box.put(_rec("progress", 3))
    box.put(_rec("progress", 4))  # full: the OLDEST log goes, not the oldest record
    box.put(_rec("progress", 5))  # full: the other log goes

    assert [r["seq"] for r in box.pending()] == [1, 3, 4, 5]
    assert box.dropped == 2 and box.dropped_logs == 2 and box.dropped_progress == 0


def test_with_no_log_left_the_oldest_progress_goes_and_a_new_log_is_refused():
    box = Outbox(live_max=3)
    for seq in range(3):
        box.put(_rec("progress", seq))
    box.put(_rec("log", 3))  # nothing droppable ranks below it: it is the one dropped
    assert [r["seq"] for r in box.pending()] == [0, 1, 2]
    box.put(_rec("progress", 4))  # now the oldest progress goes
    assert [r["seq"] for r in box.pending()] == [1, 2, 4]
    assert box.dropped_logs == 1 and box.dropped_progress == 1


def test_status_and_result_are_never_dropped_and_keep_their_place():
    box = Outbox(live_max=2)
    seq = 0
    for _ in range(500):
        box.put(_rec("log", seq))
        seq += 1
        box.put(_rec("progress", seq))
        seq += 1
    box.put(_rec("status", seq))
    box.put(_rec("result", seq + 1))
    for kind in ("status", "result"):
        for i in range(200):
            box.put(_rec(kind, 10_000 + i + (0 if kind == "status" else 1000)))

    pending = box.pending()
    kept = [r for r in pending if r["type"] in ("status", "result")]
    assert len(kept) == 402, "a status or result was dropped"
    assert len(pending) - len(kept) == 2, "the live lanes outgrew their cap"
    # One stream, in put order, across lanes.
    frames, taken = box.take(10_000, until=lambda: True)
    assert frames == []
    assert taken == pending


def test_take_blocks_until_something_arrives():
    box = Outbox()
    got: list = []
    done = threading.Event()

    def reader():
        got.append(box.take(10, until=lambda: False))
        done.set()

    threading.Thread(target=reader, daemon=True).start()
    assert not done.wait(0.1), "take returned with nothing to send"
    box.put(_rec("log", 7))
    assert done.wait(5)
    assert got[0][1] == [_rec("log", 7)]


def test_status_and_result_reach_the_app_even_when_the_live_queue_overflowed():
    """End to end over a real socket: everything queued before the app attached,
    with far more commentary than the queue holds."""
    with Server() as server:
        client = _client(server, queue_max=5, batch_max=1000)
        seq = 0
        for _ in range(100):
            client.send(_rec("log", seq))
            seq += 1
        client.send(_rec("status", seq))
        for _ in range(100):
            seq += 1
            client.send(_rec("progress", seq))
        client.send(_rec("result", seq + 1))
        client.start()
        try:
            assert server.wait_for(Method.TELEMETRY)
        finally:
            client.stop()

    got = [
        m
        for f in server.frames
        if f.get("method") == Method.TELEMETRY
        for m in f["params"]["messages"]
    ]
    types = [m["type"] for m in got]
    assert types.count("status") == 1 and types.count("result") == 1
    assert "log" not in types, "logs outlived progress under pressure"
    assert len(got) == 5 + 2
    assert [m["seq"] for m in got] == sorted(m["seq"] for m in got)


# --- dispatch: every handler on the executor, never a socket thread ----------


class NamedExecutor:
    """Runs submitted work on one named thread, like the harness's controller."""

    def __init__(self, delay_s: float = 0.0):
        self.q: queue.Queue = queue.Queue()
        self.delay_s = delay_s
        self.ran = 0
        threading.Thread(target=self._loop, name="executor", daemon=True).start()

    def __call__(self, fn):
        self.q.put(fn)
        return True

    def _loop(self):
        while True:
            fn = self.q.get()
            if self.delay_s:
                threading.Event().wait(self.delay_s)
            self.ran += 1
            fn()


def test_hello_cancel_replay_and_ping_are_all_handled_on_the_executor():
    names: dict[str, str] = {}

    def on_cancel(who):
        names["cancel"] = threading.current_thread().name
        return {"accepted": True}

    def on_replay(a, b):
        names["replay"] = threading.current_thread().name
        return []

    executor = NamedExecutor()
    with Server() as server:
        client = _client(server, on_cancel=on_cancel, on_replay=on_replay)
        client.start(executor)
        try:
            assert server.wait_for(Method.HELLO)
            assert server.call(Method.CANCEL, {"requested_by": "kp"}, id=10).ok
            assert server.call(Method.REPLAY, {"from_seq": 0}, id=11).ok
            assert server.call(Method.PING, {}, id=12).ok
        finally:
            client.stop()

    assert names == {"cancel": "executor", "replay": "executor"}
    # hello's answer + three requests, all through it
    assert executor.ran >= 4


def test_a_refusal_handled_late_is_still_a_refusal_not_a_dropped_socket():
    """The app refuses and closes at once; the controller gets to the refusal
    only afterwards. That must still read as "refused, stop" — not as a
    connection that dropped before answering, which would be retried."""
    error = {"code": ErrorCode.INCOMPATIBLE_VERSION, "message": "no"}
    with Server(hello_error=error) as server:
        client = _client(server, backoff_s=0.01)
        client.start(NamedExecutor(delay_s=0.3))
        try:
            client._thread.join(timeout=5)
            assert not client._thread.is_alive()
        finally:
            client.stop()

    assert server.connections == 1, "a refused hello was retried"
    assert client.rejected is not None


def test_cancel_and_replay_run_on_the_harness_controller(tmp_path):
    """Through a real harness: the handlers run on the thread named
    `controller`, never the model's and never a socket thread."""
    from heartbeat import Heartbeat

    from job.harness import Harness
    from job.loader import describe_object
    from job.telemetry import PartFileWriter

    names: dict[str, str] = {}
    box: list = []
    done = threading.Event()

    h = Harness(
        "r1",
        PartFileWriter(tmp_path, "r1"),
        handle=describe_object(Heartbeat(seconds=30, hz=20), "heartbeat"),
        roll_tick_s=0.05,
    )

    def on_cancel(who):
        names["cancel"] = threading.current_thread().name
        return h.cancel(who)

    def on_replay(a, b):
        names["replay"] = threading.current_thread().name
        return h.replay(a, b)

    with Server() as server:
        h.channel = _client(server, on_cancel=on_cancel, on_replay=on_replay)

        def go():
            box.append(h.run())
            done.set()

        threading.Thread(target=go, name="model-main", daemon=True).start()
        assert server.wait_for(Method.TELEMETRY)
        replay = server.call(Method.REPLAY, {"from_seq": 0, "to_seq": 0}, id=21)
        assert replay.ok and replay.result["count"] == 1
        assert server.call(Method.CANCEL, {"requested_by": "kp"}, id=22).result["accepted"]
        assert done.wait(10), "the run did not end after a cancel over the socket"
        assert server.wait_for(Method.BYE)

    assert box[0].status == "CANCELLED"
    assert names == {"cancel": "controller", "replay": "controller"}
    # The terminal status went out live before `bye`.
    methods = server.methods()
    last_batch = [f for f in server.frames if f.get("method") == Method.TELEMETRY][-1]
    assert last_batch["params"]["messages"][-1]["terminal"] is True
    assert methods[-1] == Method.BYE
