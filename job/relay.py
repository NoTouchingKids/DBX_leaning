"""The live path's queue and drop policy.

Everything offered here has *already* been written to the durable buffer, so
this queue is allowed to drop. That is the whole point: logs are best-effort
on the live path (spec), and a browser falling behind must never be able to
stall a run or cost a durable record.

Drop policy, in order:
  1. Queue full and the incoming message is a log -> drop the incoming log.
  2. Queue full and it is progress/status/result -> evict the oldest *log*
     to make room, because those three carry meaning a log does not.
  3. Queue full of nothing but progress/status/result -> drop the oldest
     progress if the incoming one is progress (a newer sample supersedes an
     older one), otherwise drop the incoming message and count it.
Every drop is counted and reported at end of run.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Protocol, runtime_checkable

from shared.envelope import LogMessage, Message, MessageType, ProgressMessage

log = logging.getLogger(__name__)

__all__ = ["LiveChannel", "LiveRelay"]


@runtime_checkable
class LiveChannel(Protocol):
    name: str

    @property
    def is_connected(self) -> bool: ...

    async def start(self) -> None: ...

    async def send_many(self, msgs: list[Message]) -> bool:
        """Returns True if the batch was delivered. False = try the next tier."""

    async def close(self) -> None: ...


class LiveRelay:
    """Holds channels in preference order: WebSocket first, HTTP push after."""

    def __init__(
        self,
        channels: list[LiveChannel] | None = None,
        *,
        queue_max: int = 2000,
        batch_max: int = 50,
    ) -> None:
        self.channels = channels or []
        self.queue_max = queue_max
        self.batch_max = batch_max
        self._q: deque[Message] = deque()
        self._wake = asyncio.Event()
        self._stopped = False
        self.sent = 0
        self.dropped = 0
        self.dropped_logs = 0

    # --- producer side (loop thread only; Emitter does the hop) -----------

    def offer(self, msg: Message) -> None:
        if self._stopped:
            return
        if len(self._q) >= self.queue_max and not self._make_room(msg):
            self.dropped += 1
            if isinstance(msg, LogMessage):
                self.dropped_logs += 1
            return
        self._q.append(msg)
        self._wake.set()

    def _make_room(self, incoming: Message) -> bool:
        if isinstance(incoming, LogMessage):
            return False  # logs yield first, by contract
        for i, queued in enumerate(self._q):
            if isinstance(queued, LogMessage):
                del self._q[i]
                self.dropped += 1
                self.dropped_logs += 1
                return True
        if isinstance(incoming, ProgressMessage):
            for i, queued in enumerate(self._q):
                if isinstance(queued, ProgressMessage):
                    del self._q[i]  # a newer sample supersedes an older one
                    self.dropped += 1
                    return True
        return False

    # --- consumer side ----------------------------------------------------

    async def pump(self) -> None:
        """Drain to the first channel that accepts the batch. Runs until stopped."""
        while not self._stopped or self._q:
            if not self._q:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=0.25)
                except (TimeoutError, asyncio.TimeoutError):
                    continue
                if self._stopped and not self._q:
                    return
                continue

            batch = [self._q.popleft() for _ in range(min(self.batch_max, len(self._q)))]
            visible = [m for m in batch if _live_visible(m)]
            if not visible:
                continue
            await self._deliver(visible)

    async def _deliver(self, batch: list[Message]) -> None:
        for channel in self.channels:
            if not channel.is_connected:
                continue
            try:
                if await channel.send_many(batch):
                    self.sent += len(batch)
                    return
            except Exception:  # noqa: BLE001 - a live channel failing is never fatal
                log.debug("live channel %s failed on send", channel.name, exc_info=True)
        # Nobody listening. The durable copy already exists; this is the
        # normal case for a run that starts while the app is down.
        self.dropped += len(batch)

    async def start(self) -> None:
        for channel in self.channels:
            try:
                await channel.start()
            except Exception:  # noqa: BLE001
                log.warning("live channel %s failed to start", channel.name, exc_info=True)

    async def stop(self) -> None:
        self._stopped = True
        self._wake.set()
        for channel in self.channels:
            try:
                await channel.close()
            except Exception:  # noqa: BLE001
                log.debug("live channel %s failed to close", channel.name, exc_info=True)

    @property
    def connected(self) -> bool:
        return any(c.is_connected for c in self.channels)

    @property
    def pending(self) -> int:
        return len(self._q)


def _live_visible(msg: Message) -> bool:
    """``client_visible=False`` filters the live send only — the durable
    write already happened, unconditionally, before this queue saw it."""
    return not (msg.type is MessageType.LOG and not msg.client_visible)
