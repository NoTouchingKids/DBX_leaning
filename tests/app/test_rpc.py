"""The app's RPC endpoint: where a job attaches.

Replaces `test_ingest_and_cancel.py`, which tested v3's msgpack control frames
and an HTTP push fallback that no longer exist. The interesting cases here are
the ones v3 could not have: an acknowledged cancel, a replay carrying records
back, and what happens to a call when the job vanishes mid-flight.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from shared.rpc import ErrorCode, Method, notification, request


def _client(app_and_hub, **cfg_kw):
    from server.config import AppConfig

    base = dict(
        catalog="main", schema="dbx_leaning", sse_keepalive_s=0.05, reconcile_on_startup=False
    )
    base.update(cfg_kw)
    app, hub = app_and_hub(AppConfig(**base))
    return TestClient(app), hub


def _msg(seq, run_id="r1", **extra):
    return {
        "type": "log",
        "run_id": run_id,
        "seq": seq,
        "ts": 1000 + seq,
        "message": f"line {seq}",
        "level": "INFO",
        "source": "model",
        "phase": "run",
        "client_visible": True,
        **extra,
    }


def test_a_job_attaches_says_hello_and_is_acknowledged(app_and_hub):
    client, hub = _client(app_and_hub)
    with client.websocket_connect("/ws/job/r1") as ws:
        ws.send_text(request(Method.HELLO, {"run_id": "r1", "next_seq": 4000}, id=1).decode())
        reply = json.loads(ws.receive_text())
        assert reply["id"] == 1
        assert reply["result"]["observed"] is True
        assert hub.job_sockets.is_connected("r1")


def test_telemetry_is_ingested_and_never_acknowledged(app_and_hub):
    """A reply to a notification is a protocol error, not merely wasteful —
    and acknowledging every batch would double the frames to confirm something
    the volume already guarantees."""
    client, hub = _client(app_and_hub)
    seen = []
    hub.ingest = lambda run_id, msg: seen.append(msg) or _noop()

    with client.websocket_connect("/ws/job/r1") as ws:
        ws.send_text(
            notification(
                Method.TELEMETRY, {"run_id": "r1", "messages": [_msg(0), _msg(1)]}
            ).decode()
        )
        # Prove nothing came back by asking something that DOES reply and
        # checking the reply is that one — a bare "no message" assertion would
        # only prove the socket was slow.
        ws.send_text(request(Method.PING, {}, id=7).decode())
        reply = json.loads(ws.receive_text())
        assert reply["id"] == 7

    assert [m.seq for m in seen] == [0, 1]


async def _noop():
    return None


def test_a_message_for_another_run_is_refused(app_and_hub):
    """A job on one socket must not be able to write into another run's
    stream, however it came by the wrong id."""
    client, hub = _client(app_and_hub)
    seen = []
    hub.ingest = lambda run_id, msg: seen.append(msg) or _noop()

    with client.websocket_connect("/ws/job/r1") as ws:
        ws.send_text(
            notification(
                Method.TELEMETRY,
                {"run_id": "r1", "messages": [_msg(0, run_id="r1"), _msg(1, run_id="OTHER")]},
            ).decode()
        )
        ws.send_text(request(Method.PING, {}, id=1).decode())
        json.loads(ws.receive_text())

    assert [m.seq for m in seen] == [0]


def test_a_malformed_frame_gets_an_error_rather_than_closing_the_socket(app_and_hub):
    """One bad frame must not cost a run its live channel."""
    client, _ = _client(app_and_hub)
    with client.websocket_connect("/ws/job/r1") as ws:
        ws.send_text("{not json")
        reply = json.loads(ws.receive_text())
        assert reply["error"]["code"] == ErrorCode.PARSE_ERROR

        ws.send_text(request(Method.PING, {}, id=2).decode())
        assert json.loads(ws.receive_text())["id"] == 2, "the socket did not survive"


def test_an_unknown_method_is_refused_by_name(app_and_hub):
    client, _ = _client(app_and_hub)
    with client.websocket_connect("/ws/job/r1") as ws:
        ws.send_text(request("escalate", {}, id=3).decode())
        reply = json.loads(ws.receive_text())
        assert reply["error"]["code"] == ErrorCode.METHOD_NOT_FOUND


# --- app-initiated calls ---------------------------------------------------
#
# These are split from the socket tests above, and not by preference.
# `TestClient` runs the app in a portal and cannot service an HTTP request
# while a `websocket_connect` session is open on the same client — the two
# deadlock. So the round trip is exercised where it actually lives (the future
# parked by `call()`, completed by `resolve()`), and the routes are checked
# against a stubbed socket layer. Between them that covers both halves; a
# genuinely end-to-end version needs a real server, which Slice 1's deploy is.


def test_a_call_parks_a_future_that_the_jobs_reply_completes():
    """The mechanism behind cancel and replay.

    A request the app makes is answered on the same socket, interleaved with
    whatever telemetry the job is streaming — so `call()` parks a future under
    the request id and the receive loop hands the reply back by id. This is
    that, without a transport in the way.
    """
    import asyncio

    from server.services import JobConnections
    from shared.rpc import Response

    async def scenario():
        sockets = JobConnections()
        sent: list[str] = []

        class Socket:
            async def send_text(self, text):
                sent.append(text)

        sockets.register("r1", Socket())
        task = asyncio.create_task(sockets.call("r1", Method.CANCEL, {"requested_by": "kp"}))
        await asyncio.sleep(0)

        asked = json.loads(sent[0])
        assert asked["method"] == Method.CANCEL
        assert asked["params"] == {"requested_by": "kp"}
        assert "id" in asked, "cancel must be a request, or no answer can come back"

        sockets.resolve("r1", Response(asked["id"], {"accepted": True}))
        assert await task == {"accepted": True}

    asyncio.run(scenario())


def test_a_late_reply_to_a_call_nobody_is_waiting_for_is_dropped():
    """A call that already timed out leaves no future. Its answer arriving
    afterwards must not raise inside the receive loop and cost the run its
    live channel."""
    from server.services import JobConnections
    from shared.rpc import Response

    sockets = JobConnections()
    sockets.resolve("r1", Response(999, {"accepted": True}))  # must not raise


def test_the_cancel_route_returns_what_the_job_said(app_and_hub):
    """`acknowledged` is the job speaking, not the app guessing — which is the
    whole point of an ack. v3 answered optimistically, so 'the job never got
    it' and 'the job accepted it' looked identical from here."""
    client, hub = _client(app_and_hub)
    asked: dict = {}

    async def fake_call(run_id, method, params, **kw):
        asked.update(run_id=run_id, method=method, params=params)
        return {"accepted": True, "already_cancelling": False, "at_seq": 12}

    hub.job_sockets.call = fake_call

    body = client.post("/api/runs/r1/cancel").json()
    assert asked["method"] == Method.CANCEL
    assert body["cancel_requested"] is True
    assert body["acknowledged"]["at_seq"] == 12


def test_the_replay_route_returns_the_records_the_job_sent(app_and_hub):
    client, hub = _client(app_and_hub)
    asked: dict = {}

    async def fake_call(run_id, method, params, **kw):
        asked.update(method=method, params=params)
        return {"count": 3, "messages": [_msg(2), _msg(3), _msg(4)]}

    hub.job_sockets.call = fake_call

    body = client.get("/api/runs/r1/replay?from_seq=2&to_seq=4").json()
    assert asked["method"] == Method.REPLAY
    assert asked["params"] == {"from_seq": 2, "to_seq": 4}
    assert body["count"] == 3
    assert [m["seq"] for m in body["messages"]] == [2, 3, 4]


@pytest.mark.parametrize("path", ["/api/runs/r1/cancel", "/api/runs/r1/replay"])
def test_a_call_with_no_job_attached_is_a_conflict_not_a_hang(app_and_hub, path):
    """No job attached is a normal state — the run finished, or was never
    observed — and it must fail fast rather than waiting out a timeout."""
    client, _ = _client(app_and_hub)
    response = client.post(path) if path.endswith("cancel") else client.get(path)
    assert response.status_code == 409


def test_a_job_that_disconnects_mid_call_fails_the_caller_immediately(app_and_hub):
    """Otherwise the caller waits out the full timeout for an answer that can
    never arrive, because the socket carrying it is gone."""
    import asyncio

    client, hub = _client(app_and_hub)

    async def scenario():
        class DeadSocket:
            async def send_text(self, _text):
                return None

        hub.job_sockets.register("r1", DeadSocket())
        task = asyncio.create_task(hub.job_sockets.call("r1", Method.PING, {}, timeout_s=30))
        await asyncio.sleep(0.05)
        hub.job_sockets.unregister("r1")

        with pytest.raises(ConnectionError, match="disconnected"):
            await task

    asyncio.run(scenario())


def test_a_job_socket_needs_no_credential_from_the_app(app_and_hub):
    """The app authenticates nothing; the Databricks Apps proxy does.

    This replaces two tests that asserted the opposite — a shared secret on
    `X-DBX-App-Token`, rejected when absent or wrong. The proxy in front of a
    deployed app refuses anything without a Databricks OAuth token from a
    principal holding CAN_USE, so that check sat on top of a platform-enforced
    one and failed OPEN when unset: no token configured meant accept everyone.

    What this test pins is therefore a deliberate property, not an oversight —
    and `app/server/routes/rpc.py` carries the note about what it means.
    """
    client, hub = _client(app_and_hub)
    with client.websocket_connect("/ws/job/r1") as ws:
        ws.send_text(request(Method.PING, {}, id=1).decode())
        assert json.loads(ws.receive_text())["result"]["pong"] is True
        assert hub.job_sockets.is_connected("r1")


# --- triggering ------------------------------------------------------------


def test_triggering_needs_no_run_store(app_and_hub):
    """The change from v3, and the reason `/docs` works without Lakebase.

    v3 reserved a slot, launched, then attached the job run id — so a missing
    store meant no run could start. Two things retired that: a scheduled run
    never passes through this route, so a ceiling enforced only here was
    counting the wrong number; and the JOB writes its own `run_status` row, so
    registering it here would be the app writing a record it does not own.

    The ceiling still holds — Databricks holds it, and every job file sets
    `queue.enabled` so a sixth task waits rather than failing.
    """
    from server.config import AppConfig

    app, hub = app_and_hub(
        AppConfig(
            catalog="main",
            schema="dbx_leaning",
            reconcile_on_startup=False,
            job_ids={"heartbeat": 42},
        )
    )

    launched: dict = {}

    class FakeJobs:
        async def run_now(self, job_id, params):
            launched.update(job_id=job_id, params=params)
            return 987654

    hub.jobs_api = FakeJobs()
    assert hub.store is None, "precondition: this test is about having no store"

    response = TestClient(app).post(
        "/api/runs",
        json={"model": "heartbeat", "config": {"seconds": 180, "hz": 1}, "run_id": "hb-001"},
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["run_id"] == "hb-001"
    assert body["job_run_id"] == 987654
    # The link a human actually wants back: where to watch what they started.
    assert body["watch"] == "/?run=hb-001"

    assert launched["job_id"] == 42
    assert launched["params"]["DBX_MODEL"] == "heartbeat"
    assert json.loads(launched["params"]["DBX_MODEL_CONFIG"]) == {"seconds": 180, "hz": 1}


def test_an_unknown_model_names_what_was_discovered(app_and_hub):
    """A 404 here almost always means discovery found nothing, not that the
    caller typed the name wrong — so say which models exist."""
    from server.config import AppConfig

    app, hub = app_and_hub(
        AppConfig(catalog="main", schema="dbx_leaning", reconcile_on_startup=False)
    )

    class FakeJobs:
        async def run_now(self, job_id, params):  # pragma: no cover - never reached
            raise AssertionError("should not launch")

    hub.jobs_api = FakeJobs()

    response = TestClient(app).post("/api/runs", json={"model": "nope"})
    assert response.status_code == 404
    assert "check the project tag" in response.json()["detail"]
