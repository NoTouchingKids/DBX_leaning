"""`run_local` with a live channel — the notebook's version of a job.

The point of these is that a notebook and a deployed job use the SAME wiring
(`job.ws.app_client`), so a notebook that proves the ingress works has proved
something about the job. A second code path that merely looked equivalent would
make the notebook a test of itself.

The server here is a real `websockets` server on a loopback port, so the frames
are real JSON-RPC over a real socket. What it is NOT is Databricks Apps: there
is no OAuth proxy, so nothing here says anything about the 302 that a missing
CAN_USE grant produces. That is only answerable against a deployed app.
"""

from __future__ import annotations

import json
import threading

import pytest

from job.local import run_local
from job.ws import ws_url_for
from shared.rpc import Method

websockets = pytest.importorskip("websockets")

#: Long enough that the socket thread certainly gets to run.
#:
#: `RpcClient.start()` spawns a thread whose loop begins `while not
#: self._stop.is_set()`, and `run_local` calls `stop()` as soon as the model
#: finishes. A 0.3s run can therefore end before that thread is first
#: scheduled, leaving `connects=0` and `last_error=None` — nothing attempted,
#: nothing to report. It passed on Linux and failed every time on Windows,
#: which is the shape of a race rather than a flake.
#:
#: Two seconds is not a tuned sleep; a loopback connect is sub-millisecond, so
#: this is several orders of magnitude of headroom. Real runs last minutes and
#: never come near this.
RUN_SECONDS = 2.0
RUN_HZ = 5


class Collector:
    """A stand-in app: accepts a socket, records every frame it is sent."""

    def __init__(self) -> None:
        self.frames: list[dict] = []
        self.messages: list[dict] = []
        self.connections = 0
        self._server = None
        self._thread: threading.Thread | None = None
        self.port = 0

    def __enter__(self) -> Collector:
        from websockets.sync.server import serve

        def handler(ws):
            self.connections += 1
            try:
                for raw in ws:
                    frame = json.loads(raw)
                    self.frames.append(frame)
                    if frame.get("method") == Method.TELEMETRY:
                        self.messages.extend(frame["params"]["messages"])
                    # A request needs a reply; a notification must not get one.
                    if "id" in frame:
                        ws.send(json.dumps({"jsonrpc": "2.0", "id": frame["id"], "result": {}}))
            except Exception:  # noqa: BLE001 - a closed socket ends the handler
                pass

        self._server = serve(handler, "127.0.0.1", 0)
        self.port = self._server.socket.getsockname()[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        if self._server is not None:
            self._server.shutdown()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def test_telemetry_reaches_the_app_over_a_real_socket(tmp_path):
    with Collector() as app:
        run = run_local(
            "heartbeat",
            run_id="live-1",
            seconds=RUN_SECONDS,
            hz=RUN_HZ,
            telemetry_dir=tmp_path,
            app_url=app.url,
            roll_every=0.05,
        )

    assert run.outcome.status == "SUCCEEDED"
    assert run.observed, f"nothing arrived: {run.last_error}"
    assert run.connects >= 1

    # The durable record and the live one describe the same run. Not
    # necessarily the same LENGTH — logs are best-effort on the live path and
    # the socket closes when the run ends — but every seq that arrived must be
    # one the run actually issued.
    durable = {m["seq"] for m in run.messages}
    live = {m["seq"] for m in app.messages}
    assert live <= durable, (
        f"the app received seqs the durable record has no trace of: {live - durable}"
    )
    assert app.messages, "connected but delivered nothing"


def test_the_session_opens_with_hello_carrying_the_resume_point(tmp_path):
    """`hello` is what lets an app that reconnects mid-run know where it is."""
    with Collector() as app:
        run_local(
            "heartbeat",
            run_id="live-2",
            seconds=RUN_SECONDS,
            hz=RUN_HZ,
            telemetry_dir=tmp_path,
            app_url=app.url,
        )

    hello = [f for f in app.frames if f.get("method") == Method.HELLO]
    assert hello, [f.get("method") for f in app.frames]
    assert hello[0]["params"]["run_id"] == "live-2"
    assert "next_seq" in hello[0]["params"]
    assert "id" in hello[0], "hello is a REQUEST — it expects an ack"


def test_a_dead_app_does_not_fail_the_run(tmp_path):
    """The rule the whole live path is built on, and the one worth a test.

    An unreachable app must leave the run SUCCEEDED with nothing delivered. A
    notebook pointed at the wrong URL, an app that is down, a missing grant:
    all of them are Tuesday, and a run that died because nobody was watching
    would be the tail wagging the dog.
    """
    run = run_local(
        "heartbeat",
        run_id="live-3",
        seconds=RUN_SECONDS,
        hz=RUN_HZ,
        telemetry_dir=tmp_path,
        # Nothing is listening here.
        app_url="http://127.0.0.1:9",
    )

    assert run.outcome.status == "SUCCEEDED"
    assert run.delivered == 0
    assert run.observed is False
    assert run.last_error, "a failed channel should say what went wrong"
    # The durable path is untouched by any of it.
    assert len(run.messages) > 0
    assert run.outcome.unflushed == 0


def test_a_local_callback_and_a_socket_can_both_be_used(tmp_path):
    """Passing `on_message` alongside `app_url` must not silently drop it —
    the local view is the one a notebook can actually see."""
    seen: list[dict] = []
    with Collector() as app:
        run = run_local(
            "heartbeat",
            run_id="live-4",
            seconds=RUN_SECONDS,
            hz=RUN_HZ,
            telemetry_dir=tmp_path,
            app_url=app.url,
            on_message=seen.append,
        )

    assert seen, "the local callback was dropped when a socket was configured"
    assert len(seen) == run.outcome.live_offered
    assert run.observed


def test_the_notebook_and_the_job_derive_the_same_url():
    """`JobConfig.ws_url` delegates to `ws_url_for`, so they cannot disagree.

    Two derivations of this failed in the least useful way available: the
    notebook proves the ingress works, the job connects somewhere else, and
    nothing errors anywhere.
    """
    from job.config import JobConfig

    cfg = JobConfig.from_env(
        {"DBX_APP_URL": "https://app.example.com/", "DBX_RUN_ID": "r1", "DBX_MODEL": "heartbeat"}
    )
    assert cfg.ws_url == ws_url_for("https://app.example.com/", "r1")
    assert cfg.ws_url == "wss://app.example.com/ws/job/r1"
