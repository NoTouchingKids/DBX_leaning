"""What the job knows about its own run, in memory.

The job is the authority on its own state, so it stops being a thing that
only *emits* and starts being a thing that can be *asked*. Three jobs, one
object:

1. **Latest status.** One `StatusMessage`, replacing itself. The job reports
   this to Lakebase on every transition (`job/lakebase.py`), so a run's state
   is knowable whether or not any socket ever attached. The durable trace is
   separate and unconditional: every status message is also written to
   `run_events` on the normal durable path.
2. **Progress history.** Bounded, kept for the end-of-run summary and for
   answering a client that missed the middle of a run.
3. **The replay ring.** Recent messages of every type, kept so a BACKFILL
   over the WebSocket can be answered from here rather than from the SQL
   warehouse — whose cost is *uptime*, and which a reconnecting browser tab
   should never be able to wake.

Why a ring and not "whatever is still in the durable buffer": the durable
buffer is emptied on every flush, so it can only ever answer for rows Delta
does not have yet. A client whose gap straddles a flush would get half an
answer. The ring is independent of flushing and covers both sides of it.

**The two bounds are the contract**, not a threshold either side has to
remember: `replay_from_seq` is the oldest seq this can still serve, and
`flushed_through_seq` is how far Delta has caught up. The job puts both on
its `hello` and on every backfill reply, and the app decides from those —
above the ring's floor, ask the job; below it, only then go to SQL.

Thread-safe: `observe()` is called from the model's worker thread through the
emitter, `since()` from the event loop when a BACKFILL arrives.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any

from .shared.envelope import (
    TERMINAL_STATUSES,
    LogMessage,
    Message,
    ProgressMessage,
    RunStatus,
    StatusMessage,
    now_ms,
)

__all__ = ["RunRecord", "DEFAULT_REPLAY_MESSAGES", "DEFAULT_PROGRESS_HISTORY"]

#: How many recent messages stay replayable. Sized like the old live queue:
#: big enough to cover a reconnect blip or a browser tab waking up, small
#: enough that a solver spraying log lines cannot grow the job's heap.
DEFAULT_REPLAY_MESSAGES = 2000

#: Progress points kept for the end-of-run summary. Progress is sampled, not
#: per-iteration, so the busiest model here (`panel_fit`, one per group)
#: produces hundreds rather than thousands.
DEFAULT_PROGRESS_HISTORY = 5000


class RunRecord:
    def __init__(
        self,
        run_id: str,
        *,
        model: str | None = None,
        job_run_id: str | None = None,
        replay_messages: int = DEFAULT_REPLAY_MESSAGES,
        progress_history: int = DEFAULT_PROGRESS_HISTORY,
    ) -> None:
        self.run_id = run_id
        self.model = model
        self.job_run_id = job_run_id
        self.started_ts = now_ms()

        self._lock = threading.Lock()
        self._replay: deque[Message] = deque(maxlen=max(1, replay_messages))
        self._progress: deque[ProgressMessage] = deque(maxlen=max(1, progress_history))
        self._status: StatusMessage | None = None
        self._latest_progress: ProgressMessage | None = None
        self._flushed_through = -1
        self._counts: dict[str, int] = {}

    # --- writing ----------------------------------------------------------

    def observe(self, msg: Message) -> None:
        """Every message the run produces passes through here, once."""
        with self._lock:
            self._replay.append(msg)
            self._counts[msg.type.value] = self._counts.get(msg.type.value, 0) + 1
            if isinstance(msg, StatusMessage):
                self._status = msg
            elif isinstance(msg, ProgressMessage):
                self._latest_progress = msg
                self._progress.append(msg)

    def note_flushed(self, through_seq: int) -> None:
        """The durable path has caught up to ``through_seq``.

        Monotonic on purpose: flushes are per-table and land out of order, so
        the honest answer to "what can the warehouse definitely serve" is the
        high-water mark, never the last table written.
        """
        with self._lock:
            self._flushed_through = max(self._flushed_through, through_seq)

    # --- reading ----------------------------------------------------------

    @property
    def status(self) -> RunStatus | None:
        with self._lock:
            return None if self._status is None else self._status.status

    @property
    def latest_status(self) -> StatusMessage | None:
        with self._lock:
            return self._status

    @property
    def latest_progress(self) -> ProgressMessage | None:
        with self._lock:
            return self._latest_progress

    @property
    def terminal(self) -> bool:
        with self._lock:
            return self._status is not None and self._status.status in TERMINAL_STATUSES

    @property
    def flushed_through_seq(self) -> int:
        with self._lock:
            return self._flushed_through

    @property
    def replay_from_seq(self) -> int:
        """The oldest seq still replayable, or 0 when nothing has aged out.

        A client asking for anything above this gets a complete answer from
        the job. Anything below it is the warehouse's to answer, and saying
        so is the whole reason this number travels on the wire.
        """
        with self._lock:
            return self._replay[0].seq if self._replay else 0

    def since(self, after_seq: int, *, limit: int | None = None) -> tuple[list[Message], bool]:
        """``(messages, complete)`` for everything after ``after_seq``.

        ``complete`` is False when the request reached below what the ring
        still holds — the job served what it had, and the caller needs the
        warehouse for the rest. It is *not* False merely because ``limit``
        truncated the answer: a truncated page is still complete as far as it
        goes, and the caller pages on by seq.

        ``client_visible=False`` logs are withheld, because the warehouse
        backfill withholds them too (`app/server/repository.py` filters on the
        column). Two sources answering the same question differently is the
        failure this whole one-envelope design exists to prevent, and raw
        solver chatter arriving only when the job happened to still hold it
        would be exactly that.

        The seq gaps this leaves are honest: the messages exist, they are in
        Delta, and nothing on the live path was ever going to show them.
        """
        with self._lock:
            held = list(self._replay)
            oldest = held[0].seq if held else 0
        covered = not held or after_seq >= oldest - 1
        out = [m for m in held if m.seq > after_seq and _client_visible(m)]
        if limit is not None and limit >= 0:
            out = out[:limit]
        return out, covered

    def progress_rows(self) -> list[ProgressMessage]:
        with self._lock:
            return list(self._progress)

    def counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    # --- the end-of-run summary ------------------------------------------

    def summary(self, *, requested_by: str | None = None) -> dict[str, Any]:
        """The run's state as one flat row, for ``run_status`` and its history.

        The first eight keys match the ``run_status`` columns in both schemas
        — Lakebase's (``lakebase_ddl/001_run_status.sql``) and Unity Catalog's
        (``uc_ddl/001_core_tables.sql``), which agree on those eight names.

        Today the only consumer is ``LakebaseStatus.report()``, whose upsert
        binds seven of them: ``requested_by`` is deliberately not bound, because
        the app sets it when it claims the slot and the job has no business
        knowing who asked. The upsert's ``DO UPDATE`` leaves that column alone,
        so a value the app wrote survives every status the job reports.

        ``seq`` and ``ts`` are the extras, and they are *the status message's
        own* coordinates rather than columns of ``run_status``: they are what
        the append-only companion row (``run_status_history``) is deduplicated
        and ordered by, so a redelivered report appends once and a reader can
        put the transitions back in the order the job saw them — not the order
        Postgres happened to receive them in.

        With no status message yet, ``seq`` is None. That is exactly what makes
        such a row exempt from the history table's
        ``UNIQUE (run_id, seq) WHERE seq IS NOT NULL``, and it should be: there
        is no message identity to dedupe on. ``ts`` falls back to ``updated_ts``
        for the same row, because that column is NOT NULL and "when this was
        reported" is still a true answer.
        """
        with self._lock:
            status = self._status
        # One clock read, not two: ``ts`` falls back to this when no status
        # message has arrived, and a fallback stamped a hair after the
        # ``updated_ts`` it ships with would have the two rows disagreeing
        # about when the same report happened.
        updated_ts = now_ms()
        return {
            "run_id": self.run_id,
            "job_run_id": self.job_run_id,
            "model": self.model,
            "status": (status.status.value if status else RunStatus.FAILED.value),
            "detail": (status.detail if status else "no terminal status was recorded"),
            "started_ts": self.started_ts,
            "updated_ts": updated_ts,
            "requested_by": requested_by,
            "seq": (status.seq if status else None),
            "ts": (status.ts if status else updated_ts),
        }

    def describe(self) -> str:
        counts = self.counts()
        parts = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "nothing yet"
        return (
            f"{self.run_id}: {self.status.value if self.status else 'no status'} "
            f"({parts}); replayable from seq {self.replay_from_seq}, "
            f"flushed through {self.flushed_through_seq}"
        )


def _client_visible(msg: Message) -> bool:
    return not (isinstance(msg, LogMessage) and not msg.client_visible)
