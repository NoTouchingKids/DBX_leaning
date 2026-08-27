"""The two live channels, in preference order.

WebSocket first: bidirectional, so it is the only thing that can carry a
cancel command *back* to the job. HTTP push second: one-way, which is fine,
because cancellation is a separate concern with its own documented escape
hatch when no WS exists.

Both degrade to silence. An unreachable app, a rejected upgrade, no
``DBX_APP_URL`` at all — none of these may raise into the run.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any, Protocol

from .shared.codec import to_jsonable
from .shared.envelope import Message
from .shared.protocol import ControlFrame, ControlKind, hello, pack_frame, pong, unpack_frame

log = logging.getLogger(__name__)

__all__ = ["WebSocketChannel", "HttpPushChannel", "WebSocketLike", "HttpClientLike"]


class WebSocketLike(Protocol):
    """The whole surface this channel needs from a websocket.

    Stated as a Protocol rather than the concrete ``websockets`` type so the
    connection can be injected in tests without either side pretending to be
    the other — and so the contract is three methods, visibly, rather than
    whatever the library happens to expose.
    """

    async def send(self, data: bytes) -> None: ...

    async def close(self) -> None: ...

    def __aiter__(self) -> AsyncIterator[bytes | str]: ...


class ConnectLike(Protocol):
    """An async context manager yielding a ``WebSocketLike``."""

    async def __aenter__(self) -> WebSocketLike: ...

    async def __aexit__(self, *exc: object) -> bool | None: ...


class HttpClientLike(Protocol):
    async def post(self, url: str, *, json: Any = ..., headers: Any = ...) -> Any: ...

    async def aclose(self) -> None: ...


class WebSocketChannel:
    """Reconnecting WS client to the app.

    Reconnects on a timer rather than only at startup: a run that begins
    while the app is down must attach by itself once the app comes up, with
    no restart. That is the normal case here, not a recovery path.
    """

    name = "websocket"

    def __init__(
        self,
        url: str,
        run_id: str,
        *,
        token: str | None = None,
        on_control: Callable[[ControlFrame], None] | None = None,
        next_seq: Callable[[], int] | None = None,
        reconnect_s: float = 30.0,
        ping_s: float = 20.0,
        connect: Callable[..., ConnectLike] | None = None,
    ) -> None:
        self.url = url
        self.run_id = run_id
        self.token = token
        self.on_control = on_control
        self.next_seq = next_seq or (lambda: 0)
        self.reconnect_s = reconnect_s
        self.ping_s = ping_s
        self._connect = connect  # injectable for tests
        self._ws: WebSocketLike | None = None
        self._task: asyncio.Task | None = None
        self._stopped = False
        self.connect_attempts = 0
        self.connects = 0

    @property
    def is_connected(self) -> bool:
        return self._ws is not None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._maintain(), name=f"ws-{self.run_id}")

    async def send_many(self, msgs: list[Message]) -> bool:
        ws = self._ws
        if ws is None:
            return False
        try:
            for msg in msgs:
                await ws.send(pack_frame(msg))
        except Exception:  # noqa: BLE001
            log.debug("ws send failed; dropping connection", exc_info=True)
            self._ws = None
            return False
        return True

    async def close(self) -> None:
        self._stopped = True
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.send(pack_frame(ControlFrame(kind=ControlKind.BYE, run_id=self.run_id)))
            except Exception:  # noqa: BLE001
                pass
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    # --- internals --------------------------------------------------------

    async def _maintain(self) -> None:
        while not self._stopped:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - never propagate into the run
                log.info("ws session ended (%s); retrying in %ss", exc, self.reconnect_s)
            self._ws = None
            if self._stopped:
                return
            try:
                await asyncio.sleep(self.reconnect_s)
            except asyncio.CancelledError:
                raise

    async def _session(self) -> None:
        connect = self._connect or _default_connect
        self.connect_attempts += 1
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        async with connect(self.url, additional_headers=headers) as ws:
            self._ws = ws
            self.connects += 1
            await ws.send(pack_frame(hello(self.run_id, next_seq=self.next_seq())))
            log.info("ws connected to %s", self.url)
            ping = asyncio.create_task(self._ping(ws))
            try:
                async for raw in ws:
                    self._handle(raw)
            finally:
                ping.cancel()
                self._ws = None

    async def _ping(self, ws: WebSocketLike) -> None:
        # App-level ping, not a WS protocol ping: a proxy can answer a
        # protocol ping without the frame ever reaching the app, which makes
        # it useless for telling a dropped connection from a quiet one.
        from .shared.protocol import ping as ping_frame

        while True:
            await asyncio.sleep(self.ping_s)
            try:
                await ws.send(pack_frame(ping_frame(self.run_id)))
            except Exception:  # noqa: BLE001
                return

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
            ws = self._ws
            if ws is not None:
                asyncio.create_task(_safe_send(ws, pack_frame(pong(self.run_id))))
            return
        if self.on_control is not None:
            self.on_control(frame)


async def _safe_send(ws: WebSocketLike, data: bytes) -> None:
    try:
        await ws.send(data)
    except Exception:  # noqa: BLE001
        pass


def _default_connect(url: str, **kwargs: Any) -> Any:
    # Returns websockets' own connect object, which satisfies ConnectLike in
    # practice without being structurally identical to it. The Protocol is
    # there to constrain what a *test* may inject, which is where it matters.
    from websockets.asyncio.client import connect

    return connect(url, **kwargs)


class HttpPushChannel:
    """One-way fallback. Cannot carry cancel — nothing here pretends it can."""

    name = "http-push"

    def __init__(
        self,
        url: str,
        *,
        token: str | None = None,
        timeout_s: float = 10.0,
        client: HttpClientLike | None = None,
        failure_backoff_s: float = 15.0,
    ) -> None:
        self.url = url
        self.token = token
        self.timeout_s = timeout_s
        self._client: HttpClientLike | None = client
        self._owns_client = client is None
        self._unhealthy_until = 0.0
        self.failure_backoff_s = failure_backoff_s
        self.posts = 0
        self.failures = 0

    @property
    def is_connected(self) -> bool:
        loop_time = asyncio.get_event_loop().time()
        return loop_time >= self._unhealthy_until

    async def start(self) -> None:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=self.timeout_s)

    async def send_many(self, msgs: list[Message]) -> bool:
        if self._client is None:
            await self.start()
        client = self._client
        if client is None:  # start() could not build one
            return False
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        payload = {"messages": [to_jsonable(m) for m in msgs]}
        try:
            resp = await client.post(self.url, json=payload, headers=headers)
            ok = 200 <= resp.status_code < 300
        except Exception:  # noqa: BLE001
            ok = False
        if ok:
            self.posts += 1
            return True
        self.failures += 1
        # Back off rather than hammering an app that is down — the durable
        # path is already carrying everything.
        self._unhealthy_until = asyncio.get_event_loop().time() + self.failure_backoff_s
        return False

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
