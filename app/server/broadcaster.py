"""The WS -> SSE relay, behind an interface.

Workers = 1 for now, so an in-process implementation is correct today. The
interface exists anyway because at 2+ workers a job's WS connection and a
browser's SSE connection can land in different processes, and an in-process
relay then silently delivers nothing — a failure mode that produces no error,
just an empty stream. Lakebase LISTEN/NOTIFY is the documented replacement;
it should be a drop-in, not a rewrite that touches every call site.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from shared.envelope import (
    TERMINAL_STATUSES,
    Message,
    MessageType,
    ProgressMessage,
    StatusMessage,
)

log = logging.getLogger(__name__)

__all__ = ["Broadcaster", "InProcessBroadcaster", "RunSnapshot"]


class RunSnapshot:
    """What a brand-new viewer needs so the page is not blank.

    Deliberately tiny: the latest status and the latest progress point. It is
    not a replay buffer — a client wanting history calls the backfill endpoint,
    which reads Delta. Server-side ring buffers per run were considered and
    dropped (see docs/architecture.md).
    """

    __slots__ = ("status", "progress", "last_seq")

    def __init__(self) -> None:
        self.status: StatusMessage | None = None
        self.progress: ProgressMessage | None = None
        self.last_seq: int = -1

    def observe(self, msg: Message) -> None:
        self.last_seq = max(self.last_seq, msg.seq)
        if isinstance(msg, StatusMessage):
            self.status = msg
        elif isinstance(msg, ProgressMessage):
            self.progress = msg

    def messages(self) -> list[Message]:
        """In seq order, which is not the same as (status, progress) order.

        The last ``id:`` a browser reads is the one it sends back as
        ``Last-Event-ID``, and the spec says last, not highest. At the end of
        a run the terminal status has the *higher* seq than the final progress
        point, so emitting status first left the client's cursor one message
        behind where it actually was — and every reconnect and backfill
        ``after_seq`` is computed from that cursor.
        """
        latest = (m for m in (self.status, self.progress) if m is not None)
        return sorted(latest, key=lambda m: m.seq)

    @property
    def terminal(self) -> bool:
        return self.status is not None and self.status.status in TERMINAL_STATUSES


@runtime_checkable
class Broadcaster(Protocol):
    async def publish(self, run_id: str, msg: Message) -> None: ...

    def subscribe(self, run_id: str, *, after_seq: int | None = None) -> Subscription: ...

    def snapshot(self, run_id: str) -> RunSnapshot | None: ...


class Subscription:
    """One SSE connection's view of one run."""

    def __init__(self, broadcaster: InProcessBroadcaster, run_id: str, queue_max: int) -> None:
        self._b = broadcaster
        self.run_id = run_id
        self.queue: asyncio.Queue[Message | None] = asyncio.Queue(maxsize=queue_max)
        self.dropped = 0

    def offer(self, msg: Message) -> None:
        try:
            self.queue.put_nowait(msg)
        except asyncio.QueueFull:
            # A browser that has stopped reading does not get to grow this
            # process's memory. Logs go first, same rule as the job's relay.
            self.dropped += 1
            if msg.type is not MessageType.LOG:
                try:
                    self.queue.get_nowait()
                    self.queue.put_nowait(msg)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    async def __aiter__(self) -> AsyncIterator[Message]:
        while True:
            msg = await self.queue.get()
            if msg is None:
                return
            yield msg

    def close(self) -> None:
        self._b._unsubscribe(self.run_id, self)
        try:
            self.queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class InProcessBroadcaster:
    def __init__(self, *, queue_max: int = 1000) -> None:
        self.queue_max = queue_max
        self._subs: dict[str, set[Subscription]] = {}
        self._snapshots: dict[str, RunSnapshot] = {}

    async def publish(self, run_id: str, msg: Message) -> None:
        snap = self._snapshots.setdefault(run_id, RunSnapshot())
        snap.observe(msg)
        for sub in tuple(self._subs.get(run_id, ())):
            sub.offer(msg)

    def subscribe(self, run_id: str, *, after_seq: int | None = None) -> Subscription:
        sub = Subscription(self, run_id, self.queue_max)
        self._subs.setdefault(run_id, set()).add(sub)
        return sub

    def _unsubscribe(self, run_id: str, sub: Subscription) -> None:
        subs = self._subs.get(run_id)
        if subs is None:
            return
        subs.discard(sub)
        if not subs:
            self._subs.pop(run_id, None)

    def snapshot(self, run_id: str) -> RunSnapshot | None:
        return self._snapshots.get(run_id)

    def forget(self, run_id: str) -> None:
        self._snapshots.pop(run_id, None)

    @property
    def subscriber_count(self) -> int:
        return sum(len(s) for s in self._subs.values())
