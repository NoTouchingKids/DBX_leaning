"""The job's side of the RPC channel, on a thread.

One thread owns the socket. The model thread never touches it — it calls
`send()`, which drops a record on a queue and returns immediately. That is the
whole reason this is threaded rather than async: a solver blocks for minutes at
a time, and nothing about the socket should care.

**Best-effort by contract.** Nothing here may raise into a run, block the model,
or change what lands on the volume. An app that is down, unreachable, or
half-way through dying is the *normal* case — apps run ~8h/day and jobs do not.
A run with no live channel at all is not degraded; it is Tuesday.

What it does:

  * connects, says `hello` with the seq it is picking up from, and streams
    `telemetry` notifications in batches;
  * answers `cancel`, `replay` and `ping` requests from the app;
  * reconnects with backoff, counting CONSECUTIVE failures and resetting on
    every success — a naive "give up after N" would kill a healthy channel
    within minutes if the ingress cuts long-lived streams periodically, which
    community reports say it does;
  * says `bye` on a clean shutdown, so the app can tell "finished" from
    "dropped".
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

from shared.rpc import (
    ErrorCode,
    Method,
    Request,
    Response,
    RpcError,
    failure,
    notification,
    parse,
    request,
    success,
)

log = logging.getLogger(__name__)

__all__ = ["RpcClient", "app_client", "diagnose", "ws_url_for"]

#: Outbound records waiting to be batched. Bounded on purpose: if the app
#: cannot keep up, the right thing is to drop live commentary, not to grow
#: without limit inside a job that has real work to do. The volume already has
#: every record.
DEFAULT_QUEUE_MAX = 10_000

#: How many records go in one `telemetry` notification.
DEFAULT_BATCH_MAX = 200

#: Give up reconnecting after this many CONSECUTIVE failures. Reset on every
#: successful open — see the module docstring.
DEFAULT_MAX_FAILURES = 10


def diagnose(exc: BaseException) -> str:
    """Turn the ingress's unhelpful rejections into a sentence naming the cause.

    A job whose request carries no Databricks identity does not get a 401. The
    Databricks Apps proxy answers the upgrade with a **302 to the OAuth login
    page**, and what surfaces depends on whether the client follows it:

        server rejected WebSocket connection: HTTP 302
        ... /oidc/oauth2/v2.0/authorize?... isn't a valid URI:
            scheme isn't ws or wss

    Neither names the cause. v3 lost an afternoon to the second form; the
    first is what `websockets.sync` reports, and is what a real deployed run
    produced on 2026-08-31 — after this function had been written to match
    only the second, so it stayed silent through six attempts.

    Both are the same fault, and the fix is one of two things: the job has no
    Databricks identity to present (no `Authorization` header at all), or it
    has one whose principal lacks `CAN_USE` on the app.

    A **503** is a different fault wearing similar clothes, and this function
    stayed silent through nine of them on 2026-09-03 — the same way it once
    stayed silent through six 302s for matching only one of that error's two
    forms. The proxy is up; there is simply nothing behind it. Either the app
    compute is stopped (Free Edition apps stop after ~24h, and the workspace
    stops them on account status) or the app has no active deployment, which
    is its own trap: `bundle deploy` uploads the app's files WITHOUT creating
    a deployment from them, so an app can exist, be started, and still serve
    nothing until `bundle run` is issued.
    """
    text = str(exc)
    redirected = "302" in text or "oidc" in text or "authorize" in text
    if redirected:
        return (
            "the app's ingress redirected the handshake to an OAuth login page. "
            "If the line above says an identity was presented, that principal "
            "lacks CAN_USE on the app — grant it with `databricks apps "
            "set-permissions`, see 'The grant that makes it work' in "
            "deploy/README.md. If no identity was presented, that is the fault "
            "instead. The run continues unobserved either way."
        )
    if "503" in text or "502" in text or "504" in text:
        return (
            "the app's ingress is up but has nothing behind it. Check both: "
            "`databricks apps get <app>` for compute STOPPED (start it with "
            "`databricks apps start <app>`), and for active_deployment None — "
            "`bundle deploy` uploads the app's files but does not create a "
            "deployment, so `databricks bundle run <app-resource> -t <target>` "
            "is the second half. Not an auth fault: a 503 never reached the "
            "app's own token check. The run continues unobserved."
        )
    if "401" in text or "403" in text:
        return (
            "the app refused the handshake outright. The proxy redirects "
            "rather than refusing, so a 401/403 came from the app itself — "
            "which no longer authenticates anything, so suspect a route that "
            "does not exist rather than a credential."
        )
    return ""


def ws_url_for(app_url: str, run_id: str) -> str:
    """The app's WS endpoint for one run.

    One function, because a job and a notebook that derive this differently
    fail in the least useful way available: the notebook proves the ingress
    works, the job connects somewhere else, and the two disagree with no error
    anywhere. `JobConfig.ws_url` delegates here.
    """
    base = app_url.rstrip("/").replace("https://", "wss://").replace("http://", "ws://")
    return f"{base}/ws/job/{run_id}"


def app_client(
    app_url: str,
    run_id: str,
    *,
    on_cancel: Callable[[str | None], dict[str, Any]],
    on_replay: Callable[[int, int | None], list[dict[str, Any]]],
    next_seq: Callable[[], int] = lambda: 0,
    workspace_host: str | None = None,
    **kwargs: Any,
) -> RpcClient:
    """An `RpcClient` wired to a real app, with real credentials.

    Split out of `job/main.py` so a notebook gets the SAME channel a deployed
    job gets — `job/local.py` calls this. A second wiring that merely looked
    equivalent would make a notebook a test of itself rather than of the job.

    The imports are deliberately lazy. `websockets` and `databricks-sdk` are in
    the job's dependency set but nothing here should need them to be installed
    in order to be imported — the unobserved path must not depend on the
    machinery for the observed one.
    """
    from websockets.sync.client import connect

    from .auth import auth_headers

    url = ws_url_for(app_url, run_id)

    def headers() -> dict[str, str]:
        """The Databricks identity, fetched fresh per connection attempt.

        Fresh rather than once: the SDK caches and refreshes internally, and a
        reconnect an hour into a run must not present a token that expired
        forty minutes ago. One credential now — see `job/auth.py` for why the
        app's own shared secret is gone.
        """
        return auth_headers(workspace_host)

    return RpcClient(
        url,
        run_id,
        connect=lambda: connect(url, additional_headers=headers() or None),
        on_cancel=on_cancel,
        on_replay=on_replay,
        next_seq=next_seq,
        **kwargs,
    )


class RpcClient:
    """Owns the socket thread. Constructed by the harness, or not at all."""

    def __init__(
        self,
        url: str,
        run_id: str,
        *,
        connect: Callable[[], Any],
        on_cancel: Callable[[str | None], dict[str, Any]],
        on_replay: Callable[[int, int | None], list[dict[str, Any]]],
        next_seq: Callable[[], int] = lambda: 0,
        queue_max: int = DEFAULT_QUEUE_MAX,
        batch_max: int = DEFAULT_BATCH_MAX,
        max_failures: int = DEFAULT_MAX_FAILURES,
        backoff_s: float = 1.0,
    ) -> None:
        self.url = url
        self.run_id = run_id
        self._connect = connect
        self._on_cancel = on_cancel
        self._on_replay = on_replay
        self._next_seq = next_seq
        self._batch_max = batch_max
        self._max_failures = max_failures
        self._backoff_s = backoff_s

        self._q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=queue_max)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_id = 0

        self.sent = 0
        self.dropped = 0
        self.connects = 0
        self.last_error: str | None = None

    # --- what the harness calls -------------------------------------------

    def send(self, record: dict[str, Any]) -> None:
        """Queue a record. Never blocks, never raises.

        A full queue drops the OLDEST record rather than refusing the newest:
        if the channel is behind, recent telemetry is what a watching human
        wants. Nothing is lost that matters — the volume has all of it, and
        `replay` can fetch any of it back.
        """
        try:
            self._q.put_nowait(record)
        except queue.Full:
            self.dropped += 1
            try:
                self._q.get_nowait()
                self._q.put_nowait(record)
            except (queue.Empty, queue.Full):  # pragma: no cover - racing drains
                pass

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="rpc", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    # --- the socket thread -------------------------------------------------

    def _loop(self) -> None:
        failures = 0
        while not self._stop.is_set():
            if failures >= self._max_failures:
                log.info(
                    "giving up on the live channel after %d consecutive failures; "
                    "the run continues unobserved",
                    failures,
                )
                return
            try:
                with self._connect() as ws:
                    self.connects += 1
                    failures = 0  # CONSECUTIVE — reset on every success
                    self._session(ws)
            except Exception as exc:  # noqa: BLE001 - a dead channel is normal
                failures += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                hint = diagnose(exc)
                log.info(
                    "live channel attempt %d failed: %s%s",
                    failures,
                    exc,
                    f" — {hint}" if hint else "",
                )
                self._stop.wait(min(30.0, self._backoff_s * failures))

    def _session(self, ws: Any) -> None:
        ws.send(
            request(
                Method.HELLO,
                {"run_id": self.run_id, "next_seq": self._next_seq()},
                id=self._id(),
            )
        )
        while not self._stop.is_set():
            self._drain(ws)
            self._pump_inbound(ws)
            time.sleep(0.01)
        self._drain(ws)
        try:
            ws.send(notification(Method.BYE, {"run_id": self.run_id}))
        except Exception:  # noqa: BLE001 - a clean goodbye is a courtesy
            log.debug("could not say bye", exc_info=True)

    def _drain(self, ws: Any) -> None:
        """Coalesce queued records into one `telemetry` notification."""
        batch: list[dict[str, Any]] = []
        while len(batch) < self._batch_max:
            try:
                batch.append(self._q.get_nowait())
            except queue.Empty:
                break
        if not batch:
            return
        ws.send(notification(Method.TELEMETRY, {"run_id": self.run_id, "messages": batch}))
        self.sent += len(batch)

    def _pump_inbound(self, ws: Any) -> None:
        try:
            raw = ws.recv(timeout=0.01)
        except TimeoutError:
            return
        except Exception:
            raise  # a dead socket is the outer loop's problem

        try:
            frame = parse(raw)
        except RpcError as exc:
            ws.send(failure(None, exc))
            return

        if isinstance(frame, Response):
            return  # our own hello's ack; nothing to do with it yet
        self._handle(ws, frame)

    def _handle(self, ws: Any, req: Request) -> None:
        try:
            result = self._invoke(req)
        except RpcError as exc:
            if not req.is_notification:
                ws.send(failure(req.id, exc))
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("handler for %s raised", req.method)
            if not req.is_notification:
                ws.send(failure(req.id, RpcError(ErrorCode.INTERNAL_ERROR, str(exc))))
            return

        # A notification gets no reply, ever — sending one is a protocol error,
        # not merely unnecessary.
        if not req.is_notification:
            ws.send(success(req.id, result))

    def _invoke(self, req: Request) -> Any:
        if req.method == Method.PING:
            return {"pong": True, "run_id": self.run_id}

        if req.method == Method.CANCEL:
            return self._on_cancel(req.params.get("requested_by"))

        if req.method == Method.REPLAY:
            from_seq = req.params.get("from_seq")
            if not isinstance(from_seq, int):
                raise RpcError(ErrorCode.INVALID_PARAMS, "from_seq must be an integer")
            to_seq = req.params.get("to_seq")
            if to_seq is not None and not isinstance(to_seq, int):
                raise RpcError(ErrorCode.INVALID_PARAMS, "to_seq must be an integer or null")
            records = self._on_replay(from_seq, to_seq)
            return {"run_id": self.run_id, "count": len(records), "messages": records}

        raise RpcError(ErrorCode.METHOD_NOT_FOUND, f"no method {req.method!r}")

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id
