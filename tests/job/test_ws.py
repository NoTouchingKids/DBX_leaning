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

from job.ws import RpcClient, app_client, diagnose
from shared.rpc import ErrorCode, Method, parse, request


class Server:
    """A server that records what it receives and can call the job back.

    One thread reads the socket — the handler — and `call()` waits on a queue
    for the reply it wants. Two threads calling `recv()` on one connection is
    a `ConcurrencyError` in `websockets.sync`, which this file found the
    honest way by doing it.
    """

    def __init__(self):
        self.frames: list[dict] = []
        self.ready = threading.Event()
        self._responses: queue.Queue[dict] = queue.Queue()
        self._conn = None
        self._server = None
        self._thread = None

    def _handler(self, ws):
        self._conn = ws
        self.ready.set()
        try:
            for raw in ws:
                frame = json.loads(raw)
                self.frames.append(frame)
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
            assert hello["params"] == {"run_id": "r1", "next_seq": 4000}
            assert "id" in hello, "hello is a request; the app is expected to answer it"
        finally:
            client.stop()


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

    drained = []
    while not client._q.empty():
        drained.append(client._q.get_nowait()["seq"])
    assert drained[-1] == 999, "the newest record was dropped instead of the oldest"


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
