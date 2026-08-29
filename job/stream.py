"""One append-only sequence of envelope messages per run.

Today a run's messages live in three places at once: `job/buffer.py` as rows
buffered per destination table awaiting a Delta flush, `job/record.py` as a
bounded replay ring answering BACKFILL, and `job/bus.py` as a queue awaiting
a WebSocket send -- three structures, three bounding policies, three drop
rules, and the two numbers the job states on the wire (`replay_from_seq`,
`flushed_through_seq`) computed *across* them. `RunStream` is meant to become
the one structure: each consumer -- the durable flusher, a live socket, a
BACKFILL reply -- a cursor into this instead of owning a private copy. See
the bottom of this docstring, and record.py/buffer.py/bus.py themselves, for
what does not map onto that cleanly yet; this module is the primitive, not
the wiring.

This holds **messages**, not logs. Of the four message types
(`log`/`progress`/`status`/`result`), `log` is the only best-effort one --
the other three must never silently disappear. Nothing here is called a log
or treated as one; doing so would invite exactly the wrong inference about
what this may discard.

## Eviction

A message may be evicted only once it is BOTH confirmed durable (reported
via `note_flushed`) AND has aged out of the trailing replay window. Neither
alone is enough:

    evict_below = min(flushed_through_seq, head_seq - replay_window)

Read the two clauses as a union of what must be *kept*: a message survives
if it is not yet durable (`seq > flushed_through_seq`) OR it is still inside
the trailing window (`seq > head_seq - replay_window`). Taking the smaller
threshold is exactly that OR, and it is why a stalled durable consumer --
`flushed_through_seq` stuck at -1 forever -- pins `evict_below` at -1 and
evicts nothing at all, no matter how small `replay_window` is: `buffer.py`'s
durable buffer is unbounded and never drops today, which is what makes
"logs may drop live, never durably" true rather than aspirational, and a
stream that evicted an unconfirmed message out from under that guarantee
would quietly break it.

The consequence is deliberate, not a bug to fix later: if the durable path
stalls, this grows without bound rather than losing anything -- the same
trade `buffer.py` already makes. What must not happen is that trade being
silent. `head_seq`, `flushed_through_seq` and `durable_lag` stay readable at
all times specifically so a stall shows up as a number to look at, not only
as memory growth with no explanation attached.

A *live* consumer falling behind is a different, unremarkable kind of lag:
it may simply be left behind, because everything it missed is either still
in the window or already durable and recoverable by BACKFILL.
`StreamCursor.lag` reports that distance per consumer, independent of
whether what it is behind on is still actually here to read.

## Thread safety

`append` arrives on the model's worker thread, by way of the emitter, for
the common case -- and is safe from any thread. Reads happen from the event
loop (a BACKFILL reply, a live send loop) and from the durable flusher's own
thread. One `threading.Lock` guards all of it; nothing in this module
imports asyncio.
"""

from __future__ import annotations

import bisect
import threading

from .shared.envelope import LogMessage, Message

__all__ = ["RunStream", "StreamCursor", "DEFAULT_REPLAY_WINDOW"]

#: How many already-durable messages stay replayable once the durable
#: consumer has caught up. Same order of magnitude as `record.py`'s ring --
#: enough to cover a reconnect blip or a browser tab waking up.
DEFAULT_REPLAY_WINDOW = 2000


class RunStream:
    def __init__(self, run_id: str, *, replay_window: int = DEFAULT_REPLAY_WINDOW) -> None:
        self.run_id = run_id
        # >=1 so the newest message is always retained the instant anything
        # has been appended -- "nothing retained" then means exactly
        # "nothing appended yet", the same reading `replay_from_seq` gives
        # RunRecord's ring, rather than an ambiguous state that could also
        # mean "a window of zero evicted the one message that existed".
        self._replay_window = max(1, replay_window)
        self._lock = threading.Lock()
        # Kept sorted by seq at all times -- see `append`. A plain list, not
        # a deque: eviction removes a prefix in one slice (`del a[:idx]`),
        # which needs slicing, and `bisect` needs random access for the
        # search either way.
        self._messages: list[Message] = []
        #: Highest seq seen, whatever order it arrived in. -1 = nothing yet.
        self._head_seq = -1
        #: The durable consumer's last reported position. -1 = nothing
        #: confirmed yet. Set only by `note_flushed`, never inferred from a
        #: cursor's read position -- see that method.
        self._flushed_through = -1

    # --- the producer: the model's worker thread, via the emitter ---------

    def append(self, msg: Message) -> None:
        """Add one message. Seq arrives already assigned by the harness's
        `SeqCounter`; this never assigns one.

        Inserted by seq, not appended blindly to the tail. Assigning a seq
        (`SeqCounter.next()`) and calling this are two separate operations,
        so two threads racing through `emit()` can have the higher seq land
        here before the lower one -- the counter hands them out in order,
        but nothing guarantees the callers reach this line in that order. A
        blind tail-append would leave the list unsorted, and every read
        below trusts sortedness to binary-search a position rather than
        scan for one; unsorted, they would not raise, they would silently
        serve a wrong or incomplete page.
        """
        if msg.run_id != self.run_id:
            raise ValueError(
                f"message stamped for run {msg.run_id!r} appended to the stream for {self.run_id!r}"
            )
        with self._lock:
            idx = bisect.bisect_left(self._messages, msg.seq, key=_seq)
            if idx < len(self._messages) and self._messages[idx].seq == msg.seq:
                raise ValueError(
                    f"seq {msg.seq} already present; SeqCounter must never repeat a value"
                )
            self._messages.insert(idx, msg)
            self._head_seq = max(self._head_seq, msg.seq)
            self._evict_locked()

    # --- the durable consumer's position -----------------------------------

    def note_flushed(self, through_seq: int) -> None:
        """Report that everything at or below `through_seq` is now durable.

        Deliberately a separate call, never inferred from a cursor's `take`.
        `take` means "handed to this consumer", not "written" -- a durable
        write can still fail after the batch is in hand, and the retry has
        to be able to try again with rows this stream may since have grown
        past. Call this only once a write actually succeeds, with the
        highest seq it covered (`DurableSink.flushed_through_seq`, or its
        successor, already computes that number correctly across tables
        that flush independently -- see `buffer.py`'s `min_pending_seq`).

        Monotonic, like `RunRecord.note_flushed`: flushes land per table and
        can complete out of order, so the honest answer to "what can the
        durable store definitely serve" is the high-water mark, never
        whatever was reported most recently.
        """
        with self._lock:
            self._flushed_through = max(self._flushed_through, through_seq)
            self._evict_locked()

    def _evict_locked(self) -> None:
        """Apply the eviction rule. Caller must hold `self._lock`.

        Runs after every `append` and every `note_flushed`, because either
        one can move `evict_below` forward -- see the module docstring for
        the formula and why it is a `min`, not a `max` or a sum.
        """
        floor = min(self._flushed_through, self._head_seq - self._replay_window)
        if not self._messages or self._messages[0].seq > floor:
            return
        idx = bisect.bisect_right(self._messages, floor, key=_seq)
        del self._messages[:idx]

    # --- reading: cursors, and one-shot BACKFILL ---------------------------

    def cursor(self, after_seq: int = -1) -> StreamCursor:
        """A new, independent read position, starting just after `after_seq`
        (default: the very beginning). Cheap -- a cursor holds nothing but
        an int and a reference back here, and creating one never touches the
        lock."""
        return StreamCursor(self, after_seq)

    def read(self, after_seq: int, *, limit: int | None = None) -> tuple[list[Message], bool]:
        """Everything after `after_seq`, for answering one BACKFILL request.

        `complete` is False when `after_seq` reaches below what is still
        retained: this served what it had, and the durable store is where
        the rest has to come from. It is *not* False merely because `limit`
        truncated the page -- a short page is complete as far as it goes,
        and the caller pages on from the last seq it saw.

        Withholds `client_visible=False` logs, matching the durable
        backfill's own filter (`app/server/repository.py`) -- two sources
        answering the same question differently is the failure the
        one-envelope design exists to prevent. `StreamCursor.take` does
        *not* filter these: the durable consumer's cursor is exactly the
        reader that must see them, since the durable write is the only
        place they are kept at all.
        """
        window, oldest = self._snapshot_after(after_seq)
        complete = oldest is None or after_seq >= oldest - 1
        out = [m for m in window if _client_visible(m)]
        # A negative limit is treated as "no limit", matching
        # `RunRecord.since` exactly -- callers on the wire already clamp
        # (`WebSocketBus._serve_backfill`) before a limit gets this far.
        if limit is not None and limit >= 0:
            out = out[:limit]
        return out, complete

    def _snapshot_after(self, after_seq: int) -> tuple[list[Message], int | None]:
        """Everything with seq > `after_seq`, plus the oldest seq still
        retained (`None` if nothing is) -- one locked read, so a concurrent
        append or eviction cannot land between computing the two and leave
        them describing different moments."""
        with self._lock:
            idx = bisect.bisect_right(self._messages, after_seq, key=_seq)
            oldest = self._messages[0].seq if self._messages else None
            return self._messages[idx:], oldest

    # --- observability ------------------------------------------------------

    @property
    def replay_from_seq(self) -> int:
        """The oldest seq still retained, or 0 when nothing has been
        appended yet. A request at or above this minus one gets a complete
        answer from `read`; anything older belongs to the durable store."""
        with self._lock:
            return self._messages[0].seq if self._messages else 0

    @property
    def flushed_through_seq(self) -> int:
        """The durable consumer's last reported position (`note_flushed`).
        -1 means nothing has been confirmed yet."""
        with self._lock:
            return self._flushed_through

    @property
    def head_seq(self) -> int:
        """The highest seq seen, whichever order it arrived in. -1 if
        nothing has been appended yet."""
        with self._lock:
            return self._head_seq

    @property
    def durable_lag(self) -> int:
        """How many messages have been appended since the durable consumer
        last reported in. The number this module exists to keep visible: a
        durable stall shows up here rather than only as memory growth."""
        with self._lock:
            return max(0, self._head_seq - self._flushed_through)

    def __len__(self) -> int:
        """How many messages are currently retained -- watch this over time
        to see the unbounded-growth trade in `_evict_locked` actually
        happening, rather than discovering it as a memory graph later."""
        with self._lock:
            return len(self._messages)

    def describe(self) -> str:
        with self._lock:
            n = len(self._messages)
            head, flushed = self._head_seq, self._flushed_through
        return (
            f"{self.run_id}: {n} message(s) retained from seq {self.replay_from_seq}, "
            f"head at {head}, durable through {flushed} ({max(0, head - flushed)} behind)"
        )


class StreamCursor:
    """One consumer's private read position into a `RunStream`.

    Not safe to share between threads -- a cursor belongs to whichever
    consumer created it (a socket's send loop, the durable flusher), the
    same way a database cursor is not handed to a second caller. Cursors
    never block each other and never block `append`: a slow one is simply
    left behind, and whatever it never got to is still inside the replay
    window or already durable -- recoverable, never lost, just not held
    here for it any more.
    """

    __slots__ = ("_stream", "_position")

    def __init__(self, stream: RunStream, after_seq: int = -1) -> None:
        self._stream = stream
        self._position = after_seq

    @property
    def position(self) -> int:
        """The seq of the last message this cursor was handed."""
        return self._position

    def take(self, limit: int) -> list[Message]:
        """Up to `limit` messages after the current position; advances by
        exactly as many as were actually returned -- an empty result leaves
        the position untouched rather than resetting it.

        Unfiltered: `client_visible=False` logs come through. Filtering
        them is `read`'s job for the BACKFILL contract specifically; this is
        the generic primitive both a live consumer and the durable one are
        built on, and the durable one is required to see everything.

        If eviction has moved past this cursor's position, the gap is
        skipped rather than raised or waited on -- everything evicted was,
        by construction, already durable, so nothing is lost, only no
        longer here to hand back.
        """
        window, _ = self._stream._snapshot_after(self._position)
        messages = window[: max(0, limit)]
        if messages:
            self._position = messages[-1].seq
        return messages

    @property
    def lag(self) -> int:
        """How many messages exist after this cursor's position, retained
        or not. An honest distance from the head, not a promise that every
        one of them can still be read back."""
        return max(0, self._stream.head_seq - self._position)


def _seq(msg: Message) -> int:
    return msg.seq


def _client_visible(msg: Message) -> bool:
    """`client_visible=False` filters what a *live* reader sees -- `read`
    withholds it to match the durable backfill; `StreamCursor.take` does not,
    because the durable consumer is a reader too, and it must not withhold
    the very thing it exists to persist."""
    return not (isinstance(msg, LogMessage) and not msg.client_visible)
