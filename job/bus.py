"""The WebSocket message bus between a job and the backend.

This replaces the old `channels.py` + `relay.py` pair. What went, and why:

- **The HTTP push channel.** A second one-way path that could not carry a
  cancel, could not answer a backfill, and existed only as a fallback for a
  socket that has since been proven through the Databricks Apps ingress. One
  live path is easier to reason about than two that must not diverge.
- **The channel abstraction.** A `LiveChannel` Protocol and a preference
  ordering are worth their weight with two implementations. With one they
  are ceremony.
- **The tiered drop policy.** The old relay evicted logs before progress
  before status because a dropped message was *gone*. It is not gone any
  more: everything offered here is already in the durable buffer AND in the
  job's replay ring, so the app can ask for it back (`ControlKind.BACKFILL`).
  Dropping the oldest when full is now both simpler and recoverable.

What stayed, deliberately:

- **The reconnect loop.** A run that starts while the app is down must attach
  by itself when the app comes up. Apps run ~8h/day; jobs do not.
- **The app-level ping.** Not a WS protocol ping: a proxy can answer one of
  those without the frame ever reaching the app, which makes it useless for
  telling a dropped connection from a quiet one.
- **Both ingress headers.** See `job/auth.py` — the proxy's OAuth token and
  the app's own shared secret travel separately because there is one
  `Authorization` header and two things authenticating.

**Nothing here may raise into the run.** An unreachable app, a rejected
upgrade, no `DBX_APP_URL` at all: the run proceeds and stays fully durable.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import AsyncIterator, Callable
from typing import Any, Protocol

from .auth import AppCredential, ingress_headers
from .record import RunRecord
from .shared.codec import to_jsonable
from .shared.envelope import TERMINAL_STATUSES, LogMessage, Message, StatusMessage
from .shared.protocol import (
    ControlFrame,
    ControlKind,
    backfill_result,
    hello,
    pack_frame,
    pong,
    unpack_frame,
)

log = logging.getLogger(__name__)

__all__ = ["WebSocketBus", "WebSocketLike", "ConnectLike", "DEFAULT_BACKFILL_LIMIT"]

#: Ceiling on how many messages one BACKFILL reply carries. A client with a
#: bigger gap pages by seq; a single unbounded reply frame would be a way for
#: one reconnect to allocate the whole ring into a msgpack buffer.
DEFAULT_BACKFILL_LIMIT = 500


class WebSocketLike(Protocol):
    """The whole surface this needs from a websocket.

    A Protocol rather than the concrete `websockets` type, so a test can
    inject a connection without either side pretending to be the other, and
    so the contract is visibly three methods.
    """

    async def send(self, data: bytes) -> None: ...

    async def close(self) -> None: ...

    def __aiter__(self) -> AsyncIterator[bytes | str]: ...


class ConnectLike(Protocol):
    """An async context manager yielding a `WebSocketLike`."""

    async def __aenter__(self) -> WebSocketLike: ...

    async def __aexit__(self, *exc: object) -> bool | None: ...


class WebSocketBus:
    """One socket, one run. Bidirectional, and the only live path there is."""

    name = "websocket"

    def __init__(
        self,
        url: str,
        run_id: str,
        *,
        record: RunRecord,
        token: str | None = None,
        credential: AppCredential | None = None,
        on_cancel: Callable[[str], None] | None = None,
        next_seq: Callable[[], int] | None = None,
        reconnect_s: float = 30.0,
        ping_s: float = 20.0,
        queue_max: int = 2000,
        batch_max: int = 50,
        connect: Callable[..., ConnectLike] | None = None,
    ) -> None:
        self.url = url
        self.run_id = run_id
        self.record = record
        self.token = token
        #: The Databricks identity the Apps proxy demands. Distinct from
        #: `token`, which is the app's own check — see `job/auth.py`.
        self.credential = credential
        self.on_cancel = on_cancel
        self.next_seq = next_seq or (lambda: 0)
        self.reconnect_s = reconnect_s
        self.ping_s = ping_s
        self.queue_max = max(1, queue_max)
        self.batch_max = max(1, batch_max)
        self._connect = connect  # injectable for tests

        self._ws: WebSocketLike | None = None
        self._q: deque[Message] = deque()
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._maintain_task: asyncio.Task | None = None
        self._send_task: asyncio.Task | None = None
        #: Fire-and-forget replies (pong, backfill) keep a handle: an
        #: unreferenced task can be collected before it ever runs.
        self._pending: set[asyncio.Task] = set()
        self._draining = False
        self._closed = False

        self.sent = 0
        #: Evicted because the queue was full. Recoverable by BACKFILL.
        self.dropped = 0
        #: Lost to a socket that died mid-batch. Counted apart from `dropped`
        #: because they mean different things: one is backpressure working as
        #: designed, the other is the connection failing.
        self.send_failures = 0
        self.connects = 0
        self.connect_attempts = 0
        self.backfills_served = 0

    @property
    def is_connected(self) -> bool:
        return self._ws is not None

    @property
    def pending(self) -> int:
        return len(self._q)

    # --- producer side (loop thread only; the Emitter does the hop) -------

    def offer(self, msg: Message) -> None:
        """Queue for sending. Never raises, never blocks.

        Full queue drops the oldest, whatever it is. That is safe here in a
        way it was not before: the durable copy already exists and the replay
        ring still holds it, so a dropped message is recoverable by BACKFILL
        rather than lost.
        """
        if self._closed:
            return
        if not _live_visible(msg):
            return
        while len(self._q) >= self.queue_max:
            self._q.popleft()
            self.dropped += 1
        self._q.append(msg)
        self._idle.clear()
        self._wake.set()

    # --- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        if self._maintain_task is None:
            self._maintain_task = asyncio.create_task(self._maintain(), name=f"ws-{self.run_id}")
        if self._send_task is None:
            self._send_task = asyncio.create_task(self._send_loop(), name=f"ws-send-{self.run_id}")

    async def drain(self, timeout_s: float = 5.0) -> int:
        """Stop accepting, then let what is queued actually go out.

        This is the step the old teardown did not have. `relay.stop()` closed
        the channels and *then* let the pump run, so the pump drained into a
        shut socket and counted everything as dropped — which, for a run that
        finishes faster than the socket can flush, was the whole live stream
        including the terminal status. Draining before closing is the fix,
        and the bound is what stops a wedged socket holding the run open.

        Returns how many messages were still unsent when it gave up.

        **Waits on `_idle`, never on `len(self._q)`.** An earlier version
        skipped the wait entirely when the queue looked empty, which is a
        different question from "is there anything still going out": the send
        loop pops a whole batch into a local list *before* awaiting its sends,
        so a batch in flight leaves `_q` empty. Drain returned at once,
        `close()` cancelled the send task mid-batch, and the tail of the live
        stream — terminal status included — was lost while `left` reported 0.
        It needed a yield between the last `offer()` and here to show up, and
        one existed; it cost ~45% of fast runs.

        `_idle` is the honest signal, and already was: `offer()` clears it and
        the send loop only sets it once the queue is empty AND the awaits from
        the last batch have returned. Waiting on it unconditionally is also
        free when there is genuinely nothing to do, because an already-set
        Event returns immediately.
        """
        self._draining = True
        self._wake.set()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._idle.wait(), timeout=timeout_s)
        left = len(self._q)
        if left:
            # The deadline beat the backlog. Order stays FIFO right up to here
            # — reordering the queue so the outcome went first was tried and
            # is WRONG: a client that treats a terminal status as end-of-stream
            # (the browser does) stops reading there and truncates the run.
            #
            # So the outcome goes last, as it always did, but it is no longer
            # allowed to be a casualty of the deadline: send it directly, as
            # the final frame. The client sees a gap in `seq` before it, which
            # is exactly the signal to BACKFILL — and every message in that gap
            # is durable and still in the replay ring.
            forced = await self._force_terminal()
            left = len(self._q)
            log.info(
                "%d message(s) unsent after %.1fs drain; terminal status %s, "
                "the durable copy stands and the app can BACKFILL the rest",
                left,
                timeout_s,
                "sent directly" if forced else "could not be sent",
            )
        return left

    async def _force_terminal(self) -> bool:
        """Push the run's outcome past a queue that will not drain in time."""
        ws = self._ws
        if ws is None:
            return False
        terminal = next(
            (
                m
                for m in reversed(self._q)
                if isinstance(m, StatusMessage) and m.status in TERMINAL_STATUSES
            ),
            None,
        )
        if terminal is None:
            return False
        try:
            await ws.send(pack_frame(terminal))
        except Exception:  # noqa: BLE001
            return False
        self.sent += 1
        # Take it out, or the send loop may send it again and the unsent count
        # reported to the caller includes a message that was delivered.
        with contextlib.suppress(ValueError):
            self._q.remove(terminal)
        return True

    async def close(self) -> None:
        """Say goodbye and shut down. Call `drain()` first."""
        self._closed = True
        self._draining = True
        self._wake.set()

        ws, self._ws = self._ws, None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.send(pack_frame(ControlFrame(kind=ControlKind.BYE, run_id=self.run_id)))
            with contextlib.suppress(Exception):
                await ws.close()

        for task in (self._send_task, self._maintain_task):
            if task is not None:
                task.cancel()
        pending = [t for t in (self._send_task, self._maintain_task) if t is not None]
        pending.extend(self._pending)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._send_task = self._maintain_task = None
        self._pending.clear()

    # --- sending ----------------------------------------------------------

    async def _send_loop(self) -> None:
        while True:
            if not self._q:
                self._idle.set()
                if self._draining and self._closed:
                    return
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=0.25)
                except TimeoutError:
                    pass
                continue

            ws = self._ws
            if ws is None:
                # No socket. Hold what is queued — a reconnect is on its way,
                # and the app catches up from `hello` plus BACKFILL. Give up
                # only once the run is closing.
                if self._draining:
                    self._idle.set()
                    return
                await asyncio.sleep(0.1)
                continue

            batch = [self._q.popleft() for _ in range(min(self.batch_max, len(self._q)))]
            try:
                for msg in batch:
                    await ws.send(pack_frame(msg))
            except Exception:  # noqa: BLE001 - a dead socket is not a run failure
                log.debug("ws send failed; ending the session", exc_info=True)
                self._ws = None
                self.send_failures += len(batch)
                # Closing is the part that matters. `_maintain` redials only
                # when `_session` returns, and `_session` is parked on the
                # INBOUND iterator — which a half-open socket keeps open
                # indefinitely after the write direction has failed. Marking
                # `_ws = None` alone would leave this loop spinning on a
                # connection nothing will ever re-establish.
                await _safe_close(ws)
                continue
            self.sent += len(batch)

    # --- the connection ---------------------------------------------------

    async def _maintain(self) -> None:
        while not self._closed:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - never propagate into the run
                log.info(
                    "ws session ended (%s); retrying in %ss%s",
                    exc,
                    self.reconnect_s,
                    _diagnosis(exc),
                )
            self._ws = None
            if self._closed:
                return
            await asyncio.sleep(self.reconnect_s)

    async def _session(self) -> None:
        connect = self._connect or _default_connect
        self.connect_attempts += 1
        headers = await ingress_headers(self.token, self.credential)
        async with connect(self.url, additional_headers=headers) as ws:
            self._ws = ws
            self.connects += 1
            await ws.send(pack_frame(self._hello()))
            log.info("ws connected to %s", self.url)
            ping = asyncio.create_task(self._ping(ws))
            try:
                async for raw in ws:
                    self._handle(raw)
            finally:
                ping.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await ping
                self._ws = None

    def _hello(self) -> ControlFrame:
        """Every (re)connect states where the job is and what it can replay."""
        return hello(
            self.run_id,
            job_run_id=self.record.job_run_id,
            next_seq=self.next_seq(),
            replay_from_seq=self.record.replay_from_seq,
            flushed_through_seq=self.record.flushed_through_seq,
        )

    async def _ping(self, ws: WebSocketLike) -> None:
        from .shared.protocol import ping as ping_frame

        while True:
            await asyncio.sleep(self.ping_s)
            try:
                await ws.send(pack_frame(ping_frame(self.run_id)))
            except Exception:  # noqa: BLE001
                # Same reasoning as the send loop: a ping that cannot go out
                # means the write direction is gone, and only closing makes
                # the read side end so the session can be redialled.
                self._ws = None
                await _safe_close(ws)
                return

    # --- inbound ----------------------------------------------------------

    def _handle(self, raw: bytes | str) -> None:
        try:
            frame = unpack_frame(raw if isinstance(raw, bytes) else raw.encode())
        except Exception:  # noqa: BLE001
            log.warning("undecodable inbound frame, ignoring", exc_info=True)
            return
        if not isinstance(frame, ControlFrame):
            log.warning("app sent an envelope message inbound; ignoring")
            return

        if frame.kind is ControlKind.PING:
            self._reply(pong(self.run_id))
        elif frame.kind is ControlKind.CANCEL:
            who = frame.payload.get("requested_by") or "app"
            log.info("cancel requested by %s", who)
            if self.on_cancel is not None:
                self.on_cancel(str(who))
        elif frame.kind is ControlKind.BACKFILL:
            self._serve_backfill(frame)

    def _serve_backfill(self, frame: ControlFrame) -> None:
        """Answer from memory. This is the request that never wakes SQL."""
        try:
            after_seq = int(frame.payload.get("after_seq", -1))
        except (TypeError, ValueError):
            log.warning("backfill with an unusable after_seq, ignoring")
            return
        raw_limit = frame.payload.get("limit")
        try:
            limit = DEFAULT_BACKFILL_LIMIT if raw_limit is None else int(raw_limit)
        except (TypeError, ValueError):
            limit = DEFAULT_BACKFILL_LIMIT
        limit = max(0, min(limit, DEFAULT_BACKFILL_LIMIT))

        messages, complete = self.record.since(after_seq, limit=limit)
        self.backfills_served += 1
        log.info(
            "backfill after seq %d: served %d message(s), complete=%s",
            after_seq,
            len(messages),
            complete,
        )
        self._reply(
            backfill_result(
                self.run_id,
                after_seq=after_seq,
                messages=[to_jsonable(m) for m in messages],
                complete=complete,
                replay_from_seq=self.record.replay_from_seq,
                flushed_through_seq=self.record.flushed_through_seq,
            )
        )

    def _reply(self, frame: ControlFrame) -> None:
        """Send a control frame back, out of band of the message queue.

        Tracked rather than fire-and-forget: an unreferenced task can be
        garbage-collected before it runs, which would silently lose a pong or
        a backfill answer.
        """
        ws = self._ws
        if ws is None:
            return
        task = asyncio.create_task(_safe_send(ws, pack_frame(frame)))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)


async def _safe_send(ws: WebSocketLike, data: bytes) -> None:
    with contextlib.suppress(Exception):
        await ws.send(data)


async def _safe_close(ws: WebSocketLike | None) -> None:
    if ws is None:
        return
    with contextlib.suppress(Exception):
        await ws.close()


def _live_visible(msg: Message) -> bool:
    """`client_visible=False` filters the live send only — the durable write
    and the replay ring already have it."""
    return not (isinstance(msg, LogMessage) and not msg.client_visible)


def _diagnosis(exc: Exception) -> str:
    """Translate the one failure that does not say what it means.

    An unauthenticated handshake does not come back 401. The Databricks Apps
    proxy answers it with a 302 to the OAuth login page, the client follows
    the redirect, and the error is about the URL it landed on:

        ws session ended (https://.../oidc/oauth2/v2.0/authorize?...
        isn't a valid URI: scheme isn't ws or wss)

    Nothing in that names the cause. It is not a bad URL — it is the proxy
    asking a machine to log in through a browser.
    """
    text = str(exc)
    if "/oidc/" not in text and "authorize" not in text:
        return ""
    return (
        " — that redirect is the Databricks Apps proxy rejecting the handshake: "
        "no Databricks OAuth token was accepted. See job/auth.py; the principal "
        "this job runs as needs CAN_USE on the app"
    )


def _default_connect(url: str, **kwargs: Any) -> Any:
    # Returns websockets' own connect object, which satisfies ConnectLike in
    # practice without being structurally identical to it. The Protocol is
    # there to constrain what a *test* may inject, which is where it matters.
    from websockets.asyncio.client import connect

    return connect(url, **kwargs)
