"""The job's ingress, and the one command that travels back to it."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from shared.codec import to_jsonable
from shared.envelope import make_message
from shared.protocol import ControlFrame, ControlKind, hello, pack_frame, ping, unpack_frame


def test_a_job_attaches_over_websocket_and_is_acknowledged(app_and_hub):
    app, hub = app_and_hub()
    with TestClient(app) as client:
        with client.websocket_connect("/ws/job/r1") as ws:
            ws.send_bytes(pack_frame(hello("r1", next_seq=4000)))
            ack = unpack_frame(ws.receive_bytes())
            assert isinstance(ack, ControlFrame) and ack.kind is ControlKind.HELLO_ACK
            assert hub.job_sockets.is_connected("r1")
    assert not hub.job_sockets.is_connected("r1"), "the socket should be forgotten on disconnect"


def test_messages_from_the_job_reach_subscribers(app_and_hub):
    app, hub = app_and_hub()
    with TestClient(app) as client:
        with client.websocket_connect("/ws/job/r1") as ws:
            sub = hub.broadcaster.subscribe("r1")
            ws.send_bytes(pack_frame(make_message("log", run_id="r1", seq=0, message="hello")))
            ws.send_bytes(pack_frame(ping("r1")))  # forces a round trip
            unpack_frame(ws.receive_bytes())

            assert sub.queue.qsize() == 1
            assert sub.queue.get_nowait().message == "hello"
            sub.close()


def test_a_job_cannot_publish_into_another_runs_stream(app_and_hub):
    app, hub = app_and_hub()
    with TestClient(app) as client:
        with client.websocket_connect("/ws/job/r1") as ws:
            sub = hub.broadcaster.subscribe("r2")
            ws.send_bytes(pack_frame(make_message("log", run_id="r2", seq=0, message="spoof")))
            ws.send_bytes(pack_frame(ping("r1")))
            unpack_frame(ws.receive_bytes())

            assert sub.queue.qsize() == 0
            sub.close()


def test_an_undecodable_frame_does_not_drop_the_connection(app_and_hub):
    app, hub = app_and_hub()
    with TestClient(app) as client:
        with client.websocket_connect("/ws/job/r1") as ws:
            ws.send_bytes(b"\xff\xfe not msgpack")
            ws.send_bytes(pack_frame(ping("r1")))
            assert unpack_frame(ws.receive_bytes()).kind is ControlKind.PONG


def test_cancel_is_forwarded_over_the_jobs_websocket(app_and_hub):
    app, hub = app_and_hub()
    with TestClient(app) as client:
        with client.websocket_connect("/ws/job/r1") as ws:
            resp = client.post(
                "/api/runs/r1/cancel", headers={"x-forwarded-email": "kp@example.com"}
            )
            assert resp.status_code == 200
            assert resp.json()["cancel_requested"] is True

            frame = unpack_frame(ws.receive_bytes())
            assert frame.kind is ControlKind.CANCEL
            assert frame.payload["requested_by"] == "kp@example.com"


def test_cancel_with_no_live_job_names_the_escape_hatch(app_and_hub):
    app, hub = app_and_hub()
    with TestClient(app) as client:
        resp = client.post("/api/runs/nobody-home/cancel")
    assert resp.status_code == 409
    assert "databricks jobs cancel-run" in resp.json()["detail"]


def test_cancel_never_reads_or_writes_run_status(app_and_hub):
    """Polling a table for a cancel flag would keep the warehouse awake for
    the whole run — the exact cost mistake this rewrite exists to avoid."""

    class Tripwire:
        async def query(self, *a, **kw):
            raise AssertionError("the cancel path touched SQL")

        available = True

        async def close(self): ...

    from server.repository import RunRepository
    from shared.tables import TableSet

    app, hub = app_and_hub()
    hub.sql = Tripwire()
    hub.repo = RunRepository(Tripwire(), TableSet())

    with TestClient(app) as client:
        with client.websocket_connect("/ws/job/r1"):
            assert client.post("/api/runs/r1/cancel").status_code == 200
        assert client.post("/api/runs/r1/cancel").status_code == 409


def test_http_push_is_an_equal_ingest_path(app_and_hub):
    app, hub = app_and_hub()
    messages = [
        to_jsonable(make_message("log", run_id="r1", seq=0, message="via http")),
        to_jsonable(make_message("progress", run_id="r1", seq=1, elapsed_seconds=1.0)),
    ]
    with TestClient(app) as client:
        sub = hub.broadcaster.subscribe("r1")
        resp = client.post("/api/runs/r1/push", json={"messages": messages})

    assert resp.status_code == 202 and resp.json() == {"accepted": 2, "received": 2}
    assert sub.queue.qsize() == 2
    sub.close()


def test_push_drops_malformed_messages_without_failing_the_batch(app_and_hub):
    app, hub = app_and_hub()
    good = to_jsonable(make_message("log", run_id="r1", seq=0, message="ok"))
    with TestClient(app) as client:
        resp = client.post(
            "/api/runs/r1/push", json={"messages": [good, {"type": "log"}, {"nope": 1}]}
        )
    assert resp.status_code == 202
    assert resp.json() == {"accepted": 1, "received": 3}


def test_push_rejects_a_body_that_is_not_a_message_batch(app_and_hub):
    app, hub = app_and_hub()
    with TestClient(app) as client:
        assert client.post("/api/runs/r1/push", json={"nope": True}).status_code == 400


@pytest.mark.parametrize(
    "header,expected",
    [(None, 401), ("Bearer wrong", 401), ("Bearer s3cret", 202)],
)
def test_the_job_ingress_checks_its_own_token_when_one_is_configured(
    app_and_hub, config, header, expected
):
    app, hub = app_and_hub(config(job_token="s3cret"))
    headers = {"Authorization": header} if header else {}
    with TestClient(app) as client:
        resp = client.post("/api/runs/r1/push", json={"messages": []}, headers=headers)
    assert resp.status_code == expected


def test_an_unauthorised_websocket_is_closed_rather_than_served(app_and_hub, config):
    from starlette.websockets import WebSocketDisconnect

    app, hub = app_and_hub(config(job_token="s3cret"))
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/job/r1") as ws:
                ws.receive_bytes()
    assert not hub.job_sockets.is_connected("r1")


@pytest.mark.parametrize(
    "headers,expected",
    [
        # The header a job uses now. `Authorization` belongs to the Databricks
        # Apps proxy, which lets nothing through without a Databricks OAuth
        # token — a job that put the shared secret there had its handshake
        # rejected before this app saw anything.
        ({"X-DBX-App-Token": "s3cret"}, 202),
        ({"X-DBX-App-Token": "Bearer s3cret"}, 202),
        ({"X-DBX-App-Token": "wrong"}, 401),
        # Still read, so the local dev stack (no proxy, no OAuth) works
        # unchanged and a job synced before the header moved still attaches.
        ({"Authorization": "Bearer s3cret"}, 202),
        # Both, as a real deployment sends them: OAuth for the proxy, the
        # shared secret for this check. The proxy's token is not ours.
        (
            {"Authorization": "Bearer some-oauth-token", "X-DBX-App-Token": "s3cret"},
            202,
        ),
        # And the reverse must not pass: an OAuth token is not the app's secret.
        ({"Authorization": "Bearer some-oauth-token"}, 401),
    ],
)
def test_the_shared_secret_has_its_own_header_and_the_old_one_still_works(
    app_and_hub, config, headers, expected
):
    app, _ = app_and_hub(config(job_token="s3cret"))
    with TestClient(app) as client:
        resp = client.post("/api/runs/r1/push", json={"messages": []}, headers=headers)
    assert resp.status_code == expected


def test_a_websocket_authenticates_on_the_same_header(app_and_hub, config):
    app, hub = app_and_hub(config(job_token="s3cret"))
    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws/job/r1",
            headers={"Authorization": "Bearer an-oauth-token", "X-DBX-App-Token": "s3cret"},
        ):
            assert hub.job_sockets.is_connected("r1")
