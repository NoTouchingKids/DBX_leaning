"""The job's side of the RPC channel: two threads and an outbox.

The model's thread never touches the socket. It calls `send()`, which puts a
record in the outbox and returns — never blocks, never raises. `ws.send` can
block with no timeout at all outside the closing handshake, and that is
exactly the stall a model-blocking main thread cannot afford to inherit.

**Two threads, one direction each:**

    sender    connects, says `hello`, waits for the app's answer, then blocks
              on the outbox and sends — `telemetry` batches, RPC replies,
              `bye`. The ONLY thread that calls `ws.send`.
    receiver  one per connection: blocks in `ws.recv`, parses, and hands every
              request — and `hello`'s answer — to the EXECUTOR. It runs no
              handler itself.

The executor is where handlers run. Inside a harness it is the controller
thread (`Harness` passes `controller.submit` to `start()`), so `cancel`,
`replay`, `ping` and the hello answer all run there, never on the model's
thread and never on either socket thread. With no executor — a bare client, as
in tests — handlers run on the receiver. Blocking reads throughout: no thread
here polls on a timer for work.

**Best-effort by contract.** Nothing here may raise into a run, block the model,
or change what lands on the volume. An app that is down, unreachable, or
half-way through dying is the *normal* case — apps run ~8h/day and jobs do not.
A run with no live channel at all is not degraded; it is Tuesday.

What it does:

  * connects, says `hello` with the seq it is picking up from and its
    protocol version, waits for the app to accept it, and only then streams
    `telemetry` notifications in batches. A refused `hello` means the run goes
    unobserved — logged once, never retried, never a run failure;
  * answers `cancel`, `replay` and `ping` requests from the app;
  * drops live records under pressure by type — `log` first, then the oldest
    `progress`, and never `status` or `result` (see `Outbox`);
  * reconnects with backoff, counting CONSECUTIVE failures and resetting on
    every success — a naive "give up after N" would kill a healthy channel
    within minutes if the ingress cuts long-lived streams periodically, which
    community reports say it does;
  * says `bye` on a clean shutdown, so the app can tell "finished" from
    "dropped". Every frame is a TEXT frame.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
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

__all__ = ["HelloRejected", "Outbox", "RpcClient", "app_client", "diagnose", "ws_url_for"]

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


#: Live `log`/`progress` records waiting to be batched. Bounded on purpose: if
#: the app cannot keep up, the right thing is to drop live commentary, not to
#: grow without limit inside a job that has real work to do. The volume already
#: has every record. `status` and `result` are not counted against this and
#: are never dropped — see `Outbox`.
DEFAULT_QUEUE_MAX = 10_000

#: How long a blocked sender sleeps at most before re-checking its exits. A
#: liveness backstop only: every state change that should wake it (a record, a
#: reply, stop, a closed socket, hello's answer) notifies it directly.
_BACKSTOP_S = 1.0

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


#: Types that are never dropped from the live path: they travel in their own
#: lane with no cap. Safe because a run has only a handful of them.
KEPT_TYPES = frozenset({"status", "result"})


class Outbox:
    """What the sender sends: RPC replies, and records in three lanes.

    ``kept``    `status` and `result`. Unbounded, never dropped.
    ``logs``    `log`. First to go when the live lanes are full.
    ``other``   `progress`, and any type this build does not know. Its oldest
                goes once there is no `log` left to drop.

    `logs` and `other` together hold at most `live_max` records. Records leave
    in the order they were put, across all three lanes, so a batch reads in
    `seq` order whenever nothing was dropped.

    Durable writes never come through here: the harness appends to the part
    files before it offers a record to the live channel, so nothing this class
    drops is lost — only late.
    """

    def __init__(self, live_max: int = DEFAULT_QUEUE_MAX) -> None:
        self._cond = threading.Condition()
        self._live_max = max(1, live_max)
        self._n = 0
        self._kept: deque[tuple[int, dict[str, Any]]] = deque()
        self._logs: deque[tuple[int, dict[str, Any]]] = deque()
        self._other: deque[tuple[int, dict[str, Any]]] = deque()
        #: RPC replies, each tagged with the session it answers — a reply
        #: meant for a connection that has since dropped is discarded, never
        #: sent down a new one.
        self._frames: deque[tuple[object, str]] = deque()

        self.dropped = 0
        self.dropped_logs = 0
        self.dropped_progress = 0

    def put(self, record: dict[str, Any]) -> None:
        """Queue one record. Never blocks.

        **The drop point.** Live delivery is best-effort for EVERY type, and
        `replay` is how a client catches up: a record dropped here is already
        in the part files, and `replay(from_seq, to_seq)` serves it from
        there. So what this decides is what a client sees FIRST, not what it
        can HAVE. Signed off 2026-09-24: when the live lanes are full, the
        oldest `log` goes; if there is none, an incoming `log` is itself the
        one dropped; otherwise the oldest `progress` goes. `status` and
        `result` are never dropped here.
        """
        kind = record.get("type") if isinstance(record, dict) else None
        with self._cond:
            self._n += 1
            item = (self._n, record)
            if kind in KEPT_TYPES:
                self._kept.append(item)
            else:
                if len(self._logs) + len(self._other) >= self._live_max:
                    self.dropped += 1
                    if self._logs:
                        self._logs.popleft()
                        self.dropped_logs += 1
                    elif kind == "log":
                        self.dropped_logs += 1
                        return
                    else:
                        self._other.popleft()
                        self.dropped_progress += 1
                (self._logs if kind == "log" else self._other).append(item)
            self._cond.notify_all()

    def put_frame(self, frame: str, session: object) -> None:
        with self._cond:
            self._frames.append((session, frame))
            self._cond.notify_all()

    def wake(self) -> None:
        """Rouse a blocked `take` so it re-checks why it is waiting."""
        with self._cond:
            self._cond.notify_all()

    def discard_frames(self) -> None:
        with self._cond:
            self._frames.clear()

    def take(
        self, max_records: int, *, until: Callable[[], bool]
    ) -> tuple[list[tuple[object, str]], list[dict[str, Any]]]:
        """Block until there is something to send or `until()` is true, then
        return every queued reply and up to `max_records` records in order."""
        with self._cond:
            while not self._has_any_locked() and not until():
                self._cond.wait(_BACKSTOP_S)
            frames = list(self._frames)
            self._frames.clear()
            records: list[dict[str, Any]] = []
            while len(records) < max_records:
                lanes = [lane for lane in (self._kept, self._logs, self._other) if lane]
                if not lanes:
                    break
                records.append(min(lanes, key=lambda lane: lane[0][0]).popleft()[1])
        return frames, records

    def empty(self) -> bool:
        with self._cond:
            return not self._has_any_locked()

    def pending(self) -> list[dict[str, Any]]:
        """The queued records, in the order they would be sent. Diagnostic."""
        with self._cond:
            items = [*self._kept, *self._logs, *self._other]
        return [record for _, record in sorted(items, key=lambda item: item[0])]

    def __len__(self) -> int:
        with self._cond:
            return len(self._kept) + len(self._logs) + len(self._other)

    def _has_any_locked(self) -> bool:
        return bool(self._frames or self._kept or self._logs or self._other)


class _Session:
    """One connection: its socket, its `hello`, and what has been heard back.

    Written by the receiver (`answer_seen`, `closed`) and by whichever thread
    handles hello's answer (`accepted`, `rejected`); read by the sender, which
    waits on `cond` for any of them to change.
    """

    def __init__(self, ws: Any, hello_id: int) -> None:
        self.ws = ws
        self.hello_id = hello_id
        self.cond = threading.Condition()
        self.answer_seen = False
        self.accepted = False
        self.rejected: dict[str, Any] | None = None
        self.closed = False

    def poke(self) -> None:
        with self.cond:
            self.cond.notify_all()


class RpcClient:
    """The live channel to the app. Constructed by `job/main.py`, or not at all.

    Satisfies the harness's `LiveChannel`: `send` from the model's thread,
    `start(executor)` and `close(timeout)` from the harness's run.
    """

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
        executor: Callable[[Callable[[], Any]], Any] | None = None,
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
        self._executor = executor

        self.outbox = Outbox(queue_max)
        self._stop = threading.Event()
        #: The SENDER thread. The receiver is per connection.
        self._thread: threading.Thread | None = None
        self._receiver: threading.Thread | None = None
        self._session: _Session | None = None
        self._next_id = 0

        self.sent = 0
        self.connects = 0
        self.last_error: str | None = None
        #: The app's error object if it refused our `hello`; None otherwise.
        self.rejected: dict[str, Any] | None = None

    @property
    def dropped(self) -> int:
        """Live records dropped under pressure. All of them are in the part
        files; `replay` serves them."""
        return self.outbox.dropped

    # --- what the harness calls -------------------------------------------

    def send(self, record: dict[str, Any]) -> None:
        """Queue a record. Never blocks, never raises, never touches the
        socket — see `Outbox.put` for what a full queue drops."""
        try:
            self.outbox.put(record)
        except Exception:  # noqa: BLE001 - the live path cannot fail a run
            log.debug("could not queue a live record", exc_info=True)

    def start(self, executor: Callable[[Callable[[], Any]], Any] | None = None) -> None:
        """Start the sender. `executor` is where inbound handlers will run —
        the harness passes its controller's `submit`. Idempotent."""
        if executor is not None:
            self._executor = executor
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._send_loop, name="rpc-send", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Flush what is queued, say `bye`, close. Joins for at most `timeout`."""
        self._stop.set()
        self.outbox.wake()
        session = self._session
        if session is not None:
            session.poke()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def close(self, timeout: float = 5.0) -> None:
        """The harness's name for `stop` — its last shutdown step."""
        self.stop(timeout)

    # --- the sender thread -------------------------------------------------

    def _send_loop(self) -> None:
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
                    self._session_run(ws)
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
            finally:
                self._join_receiver()

    def _session_run(self, ws: Any) -> None:
        session = _Session(ws, self._id())
        self.outbox.discard_frames()  # replies to a previous connection's requests
        self._session = session
        try:
            self._receiver = threading.Thread(
                target=self._recv_loop, args=(session,), name="rpc-recv", daemon=True
            )
            self._receiver.start()
            ws.send(
                request(
                    Method.HELLO,
                    {
                        "run_id": self.run_id,
                        "next_seq": self._next_seq(),
                        "protocol_version": PROTOCOL_VERSION,
                        "capabilities": JOB_CAPABILITIES,
                    },
                    id=session.hello_id,
                )
            )
            self._await_hello(session)
            self._stream(session)
        finally:
            self._session = None

    def _await_hello(self, session: _Session) -> None:
        """Wait for the app's answer to `hello` before streaming anything.

        The app processes nothing until `hello` is accepted, and closes the
        socket if it is refused — so sending telemetry first would race that
        close and turn a final refusal into an ordinary dropped connection,
        retried forever. Raises `HelloRejected` on an error reply; a timeout
        raises `TimeoutError`, which the outer loop counts as a normal failure.

        The answer is HANDLED on the executor (the controller), so a socket
        the app closes straight after refusing can be seen closed here before
        the refusal is processed. `answer_seen` — set by the receiver the
        moment it reads the answer — is what stops that from being mistaken
        for a dropped connection and retried.
        """
        deadline = time.monotonic() + self._hello_timeout_s
        with session.cond:
            while True:
                if session.rejected is not None:
                    raise HelloRejected(session.rejected)
                if session.accepted:
                    return
                if self._stop.is_set():
                    raise TimeoutError("stopped before the app answered hello")
                if session.closed and not session.answer_seen:
                    raise ConnectionError("the app closed the socket before answering hello")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"app did not answer hello in {self._hello_timeout_s}s")
                session.cond.wait(remaining)

    def _stream(self, session: _Session) -> None:
        """Send until stopped; on stop, flush everything queued, then `bye`."""
        ws = session.ws

        def done() -> bool:
            return self._stop.is_set() or session.closed

        while True:
            frames, records = self.outbox.take(self._batch_max, until=done)
            for owner, frame in frames:
                if owner is session:
                    ws.send(frame)
            if records:
                ws.send(
                    notification(Method.TELEMETRY, {"run_id": self.run_id, "messages": records})
                )
                self.sent += len(records)
            if session.closed:
                raise ConnectionError("the app closed the socket")
            if self._stop.is_set() and self.outbox.empty():
                break
        try:
            ws.send(notification(Method.BYE, {"run_id": self.run_id}))
        except Exception:  # noqa: BLE001 - a clean goodbye is a courtesy
            log.debug("could not say bye", exc_info=True)

    def _join_receiver(self) -> None:
        receiver, self._receiver = self._receiver, None
        if receiver is not None and receiver is not threading.current_thread():
            receiver.join(timeout=5)

    # --- the receiver thread -----------------------------------------------

    def _recv_loop(self, session: _Session) -> None:
        try:
            while True:
                self._on_frame(session, session.ws.recv())
        except Exception:  # noqa: BLE001 - a closed socket ends the receiver
            log.debug("receiver ended", exc_info=True)
        finally:
            with session.cond:
                session.closed = True
                session.cond.notify_all()
            self.outbox.wake()

    def _on_frame(self, session: _Session, raw: Any) -> None:
        try:
            frame = parse(raw)
        except RpcError as exc:
            self.outbox.put_frame(failure(None, exc), session)
            return

        if isinstance(frame, Response):
            if frame.id == session.hello_id:
                with session.cond:
                    session.answer_seen = True
                self._dispatch(lambda: self._on_hello_answer(session, frame))
            return  # nothing else we sent expects an answer
        self._dispatch(lambda: self._handle(session, frame))

    def _dispatch(self, fn: Callable[[], Any]) -> None:
        """Run a handler on the executor — the controller, inside a harness.

        An executor that will not take it (stopped, or raising) means it runs
        here, on the receiver: a request is never silently left unanswered.
        """
        executor = self._executor
        if executor is not None:
            try:
                if executor(fn) is not False:
                    return
            except Exception:  # noqa: BLE001
                log.debug("executor refused a handler; running it on the receiver", exc_info=True)
        fn()

    # --- handlers: on the executor ------------------------------------------

    def _on_hello_answer(self, session: _Session, frame: Response) -> None:
        with session.cond:
            if frame.error is not None:
                session.rejected = frame.error
            else:
                session.accepted = True
                result = frame.result if isinstance(frame.result, dict) else {}
                log.info("attached to the app (app protocol %s)", result.get("protocol_version"))
            session.cond.notify_all()

    def _handle(self, session: _Session, req: Request) -> None:
        try:
            result = self._invoke(req)
        except RpcError as exc:
            if not req.is_notification:
                self.outbox.put_frame(failure(req.id, exc), session)
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("handler for %s raised", req.method)
            if not req.is_notification:
                self.outbox.put_frame(
                    failure(req.id, RpcError(ErrorCode.INTERNAL_ERROR, str(exc))), session
                )
            return

        # A notification gets no reply, ever — sending one is a protocol error,
        # not merely unnecessary.
        if not req.is_notification:
            self.outbox.put_frame(success(req.id, result), session)

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
