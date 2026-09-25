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

  * connects, says `hello` with the seq it is picking up from and its
    protocol version, waits for the app to accept it, and then streams
    `telemetry` notifications in batches. A refused `hello` means the run goes
    unobserved — logged once, never retried, never a run failure;
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
    PROTOCOL_VERSION,
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

#: The methods this job answers, sent in `hello`'s params. LSP-style: a key
#: per method, its value that method's options (none yet).
JOB_CAPABILITIES: dict[str, dict[str, Any]] = {
    Method.CANCEL: {},
    Method.REPLAY: {},
    Method.PING: {},
}

#: How long to wait for the app to answer `hello` before treating the attempt
#: as an ordinary failed connection (and retrying on the usual backoff).
DEFAULT_HELLO_TIMEOUT_S = 10.0


class HelloRejected(Exception):
    """The app answered `hello` with an error: it will not observe this job.

    Final, not transient — typically a protocol version outside the app's
    compatibility rule, which no amount of reconnecting changes. The run goes
    on unobserved and fully durable.
    """

    def __init__(self, error: dict[str, Any]) -> None:
        super().__init__(error.get("message", "hello refused"))
        self.error = error


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
    client_id: str | None = None,
    client_secret: str | None = None,
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

    from .auth import M2MTokenProvider, auth_headers

    url = ws_url_for(app_url, run_id)

    # ONE provider for the run, not one per attempt — this is what makes its
    # cache worth having. `M2MTokenProvider.token()` returns the same token
    # for a reconnect a minute in and a fresh one for a reconnect an hour in;
    # a provider rebuilt inside `headers()` would cache nothing and exchange a
    # new token on every single attempt. `None` when there are no client
    # credentials, in which case `auth_headers` never looks at it.
    m2m = (
        M2MTokenProvider(workspace_host, client_id, client_secret)
        if (workspace_host and client_id and client_secret)
        else None
    )

    def headers() -> dict[str, str]:
        """The Databricks identity, resolved fresh per connection attempt.

        "Fresh" does not mean "refetched" — `auth_headers` calls back into
        `m2m`'s own cache for the M2M path, and into the SDK's for the
        default path. What matters is that a reconnect asks again rather than
        replaying whatever it captured at the start of the run, so a token
        that expired forty minutes ago is never the one presented forty
        minutes later. See `job/auth.py` for why the app's own shared secret
        is gone and one credential is all there is now.
        """
        return auth_headers(
            workspace_host, client_id=client_id, client_secret=client_secret, m2m=m2m
        )

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
        hello_timeout_s: float = DEFAULT_HELLO_TIMEOUT_S,
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
        self._hello_timeout_s = hello_timeout_s

        self._q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=queue_max)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_id = 0

        self.sent = 0
        self.dropped = 0
        self.connects = 0
        self.last_error: str | None = None
        #: The app's error object if it refused our `hello`; None otherwise.
        self.rejected: dict[str, Any] | None = None

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
            except HelloRejected as exc:
                # NOT a run failure and NOT a reason to retry: the app has said
                # it will not observe this job, and reconnecting would get the
                # same answer forever. Say why, once, and stop.
                self.rejected = exc.error
                self.last_error = f"hello refused: {exc.error}"
                log.warning(
                    "the app refused this job's hello (protocol %s): %s — the run "
                    "continues UNOBSERVED and fully durable; no further attempts",
                    PROTOCOL_VERSION,
                    exc.error,
                )
                return
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
        hello_id = self._id()
        ws.send(
            request(
                Method.HELLO,
                {
                    "run_id": self.run_id,
                    "next_seq": self._next_seq(),
                    "protocol_version": PROTOCOL_VERSION,
                    "capabilities": JOB_CAPABILITIES,
                },
                id=hello_id,
            )
        )
        self._await_hello(ws, hello_id)
        while not self._stop.is_set():
            self._drain(ws)
            self._pump_inbound(ws)
            time.sleep(0.01)
        self._drain(ws)
        try:
            ws.send(notification(Method.BYE, {"run_id": self.run_id}))
        except Exception:  # noqa: BLE001 - a clean goodbye is a courtesy
            log.debug("could not say bye", exc_info=True)

    def _await_hello(self, ws: Any, hello_id: int) -> None:
        """Wait for the app's answer to `hello` before streaming anything.

        The app processes nothing until `hello` is accepted, and closes the
        socket if it is refused — so sending telemetry first would race that
        close and turn a final refusal into an ordinary dropped connection,
        retried forever. Raises `HelloRejected` on an error reply; a timeout
        raises `TimeoutError`, which the outer loop counts as a normal failure.
        """
        deadline = time.monotonic() + self._hello_timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"app did not answer hello in {self._hello_timeout_s}s")
            if self._stop.is_set():
                raise TimeoutError("stopped before the app answered hello")
            try:
                raw = ws.recv(timeout=min(remaining, 0.1))
            except TimeoutError:
                continue
            try:
                frame = parse(raw)
            except RpcError as exc:
                ws.send(failure(None, exc))
                continue
            if not isinstance(frame, Response):
                self._handle(ws, frame)
                continue
            if frame.id != hello_id:
                continue
            if frame.error is not None:
                raise HelloRejected(frame.error)
            result = frame.result if isinstance(frame.result, dict) else {}
            log.info("attached to the app (app protocol %s)", result.get("protocol_version"))
            return

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
            return  # hello's ack was consumed by _await_hello; nothing else is ours
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
