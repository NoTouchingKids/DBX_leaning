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

What changed again when the send queue became a cursor into `job/stream.py`'s
`RunStream` (the same structure the durable flusher and a BACKFILL reply now
read from, instead of a private deque here): there is no longer a local
buffer for this bus to bound on its own. A cursor that falls behind simply
lags; nothing here removes anything from the stream itself, because eviction
there answers to durability, not to how far any one live consumer has got.
Left alone, a disconnected or merely slow socket next to a durable path that
is *also* behind would let the run's retained history grow without the bound
`queue_max` used to guarantee. So `queue_max` is kept, and means something
adjacent rather than identical: not "how many messages this bus may hold,"
which it no longer does, but "how far behind the stream's head this bus's own
cursor may sit" — past it, the excess is skipped, not sent, counted the same
way a dropped message always was. What is skipped is never the only copy: it
is retained-or-already-durable by construction, so BACKFILL is what a client
gets it back from.

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
from collections.abc import AsyncIterator, Callable
from typing import Any, Protocol

from .auth import AppCredential, ingress_headers
from .record import RunRecord
from .shared.codec import to_jsonable
from .shared.envelope import TERMINAL_STATUSES, LogMessage, Message
from .shared.protocol import (
    ControlFrame,
    ControlKind,
    backfill_result,
    hello,
    pack_frame,
    pong,
    unpack_frame,
)
from .stream import RunStream, StreamCursor

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
        stream: RunStream | None = None,
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
        #: `job_run_id` for `hello`, and (via `latest_status`) the terminal
        #: message `_force_terminal` pushes past a queue that will not drain
        #: in time. The replay ring itself is `stream`, below.
        self.record = record
        self.token = token
        #: The Databricks identity the Apps proxy demands. Distinct from
        #: `token`, which is the app's own check — see `job/auth.py`.
        self.credential = credential
        self.on_cancel = on_cancel
        self.next_seq = next_seq or (lambda: 0)
        self.reconnect_s = reconnect_s
        self.ping_s = ping_s
        #: Was a queue capacity; now the cursor lag cap — see the module
        #: docstring. Same knob, same default, a different consequence for
        #: crossing it.
        self.queue_max = max(1, queue_max)
        self.batch_max = max(1, batch_max)
        self._connect = connect  # injectable for tests

        self._ws: WebSocketLike | None = None
        self.stream: RunStream
        self._cursor: StreamCursor
        self.rebind_stream(stream if stream is not None else RunStream(run_id))
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
        #: Skipped because the cursor fell more than `queue_max` behind the
        #: stream's head. Recoverable by BACKFILL — see the module docstring.
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
        """How far this bus's cursor sits behind the stream's head — an
        honest distance, not a promise every one of them is still here to
        send (see `StreamCursor.lag`)."""
        return self._cursor.lag

    def rebind_stream(self, stream: RunStream) -> None:
        """Point this bus at a different `RunStream`, with a fresh cursor.

        A bus constructed before the run's real stream exists — every test
        that builds one directly, and `JobHarness._build_bus` for an injected
        bus — gets a throwaway placeholder at construction time. Swapping the
        stream in later without also throwing away the old cursor would leave
        `_cursor` pointed at the placeholder forever, so a BACKFILL would
        answer from an empty stream nobody ever appends to instead of the
        run's real one. Mirrors reassigning `.record` for the same reason.
        """
        self.stream = stream
        self._cursor = stream.cursor()

    # --- producer side (loop thread only; the Emitter does the hop) -------

    def notify(self) -> None:
        """Wake the send loop: something new exists in the run's stream.

        Carries no payload. The message itself is already durable-safe the
        moment `Emitter.emit` appends it to `self.stream` — this bus reads it
        back out through its own cursor, so all a notification has to do is
        make the send loop look again promptly instead of waiting out its
        poll interval.
        """
        if self._closed:
            return
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

        **Waits on `_idle`, never on `self._cursor.lag`.** An earlier version
        (back when this held a local queue) skipped the wait entirely when the
        queue looked empty, which is a different question from "is there
        anything still going out": the send loop takes a whole batch from the
        cursor *before* awaiting its sends, so a batch in flight already reads
        as caught-up. Drain returned at once, `close()` cancelled the send
        task mid-batch, and the tail of the live stream — terminal status
        included — was lost while `left` reported 0. It needed a yield between
        the last append and here to show up, and one existed; it cost ~45% of
        fast runs.

        `_idle` is the honest signal, and already was: `notify()` clears it and
        the send loop only sets it once the cursor has caught up to the
        stream's head AND the awaits from the last batch have returned.
        Waiting on it unconditionally is also free when there is genuinely
        nothing to do, because an already-set Event returns immediately.
        """
        self._draining = True
        self._wake.set()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._idle.wait(), timeout=timeout_s)
        left = self._cursor.lag
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
            # is durable and still in the stream.
            forced = await self._force_terminal()
            # NOT `self._cursor.lag` again: `_force_terminal`, on success,
            # rebinds the cursor straight to the terminal message's own seq
            # (see there) so a future send never re-offers what this already
            # pushed out of band -- and since the terminal is normally the
            # run's last message, that rebind alone would read as `lag == 0`,
            # silently erasing everything abandoned in between from this
            # count. One message left the unsent bucket, however it left;
            # the rest are still exactly as abandoned as `left` said a line
            # above, and BACKFILL is how the app gets them back.
            if forced:
                left -= 1
            log.info(
                "%d message(s) unsent after %.1fs drain; terminal status %s, "
                "the durable copy stands and the app can BACKFILL the rest",
                left,
                timeout_s,
                "sent directly" if forced else "could not be sent",
            )
        return left

    async def _force_terminal(self) -> bool:
        """Push the run's outcome past a queue that will not drain in time.

        Reads it off `record.latest_status` rather than searching for it:
        there is no local queue to scan any more, and the record already
        keeps exactly this (see `RunRecord`'s own docstring, job #1).
        """
        ws = self._ws
        if ws is None:
            return False
        terminal = self.record.latest_status
        if terminal is None or terminal.status not in TERMINAL_STATUSES:
            return False
        if terminal.seq <= self._cursor.position:
            return False  # already sent by the ordinary path
        try:
            await ws.send(pack_frame(terminal))
        except Exception:  # noqa: BLE001
            return False
        self.sent += 1
        # Jump the cursor to this position so the unsent count reported to
        # the caller does not still include a message that was just
        # delivered — and so nothing between the old position and here gets
        # offered again later: it was deliberately skipped, not sent, and
        # BACKFILL is how the app gets it back.
        self._cursor = self.stream.cursor(terminal.seq)
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
            # Runs whether or not a socket is attached right now: a lagging
            # cursor is a memory question, not a send question, and the old
            # queue-capacity cap applied unconditionally too (see the module
            # docstring for why this must not wait on having a connection).
            self._skip_lagging_cursor()

            # `.lag` is a non-destructive peek (unlike `.take()`), which is
            # exactly what is needed to mirror the old `if not self._q` check:
            # deciding whether there is anything new must not itself consume
            # it, or the branch below (no socket) would advance the cursor
            # past messages it has nowhere to send.
            if self._cursor.lag == 0:
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
                # No socket. Hold position — a reconnect is on its way, and
                # the app catches up from `hello` plus BACKFILL. Give up only
                # once the run is closing.
                if self._draining:
                    self._idle.set()
                    return
                await asyncio.sleep(0.1)
                continue

            taken = self._cursor.take(self.batch_max)
            if not taken:
                # `.lag` said behind, but nothing was actually retained to
                # read: eviction reached this position before the cursor did.
                # Only reachable if `queue_max` here is configured larger than
                # the stream's own replay window -- production ties both to
                # the same value, so normal operation never sees this -- but
                # left unhandled, `.lag` would stay positive forever (`take`
                # does not move a cursor's position when it finds nothing),
                # and this loop has no `await` in this branch: that would be
                # a genuine busy-spin freezing the event loop, not just a
                # stuck bus. Jump to the retained floor so it cannot happen.
                self._cursor = self.stream.cursor(self.stream.replay_from_seq - 1)
                continue
            # `client_visible=False` logs come back from `take()` — it is the
            # generic primitive the durable pull is built on too, and that
            # side must see everything. Filtering here, after the cursor has
            # already advanced past them, is what keeps them off the socket;
            # `read()` (BACKFILL) does the equivalent for the same reason.
            batch = [m for m in taken if _live_visible(m)]
            if not batch:
                continue  # nothing to send this round; loop again, no wait

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

    def _skip_lagging_cursor(self) -> None:
        """Bound how far behind the stream's head this bus's cursor may sit.

        There is no local queue any more to cap on this bus's own behalf —
        `job/stream.py`'s eviction is durability-gated and does not know or
        care how far any one live consumer has fallen behind. Left alone, a
        disconnected or merely slow socket next to a durable path that is
        *also* behind would grow the run's retained history without bound, in
        a way the old fixed-size queue never allowed. Past `queue_max`, jump
        the excess rather than insisting on sending a backlog nobody would
        thank this bus for delivering late: what is skipped is retained (or
        already durable) by construction, so BACKFILL is how it comes back.
        """
        lag = self._cursor.lag
        if lag <= self.queue_max:
            return
        skipped = self._cursor.take(lag - self.queue_max)
        self.dropped += len(skipped)

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
            replay_from_seq=self.stream.replay_from_seq,
            flushed_through_seq=self.stream.flushed_through_seq,
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

        messages, complete = self.stream.read(after_seq, limit=limit)
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
                replay_from_seq=self.stream.replay_from_seq,
                flushed_through_seq=self.stream.flushed_through_seq,
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
    """`client_visible=False` filters the live send only — the stream and the
    durable write both already have it (`StreamCursor.take` is unfiltered;
    see `job/stream.py`)."""
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
