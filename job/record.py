"""What the job knows about its own run, in memory.

Used to be three jobs in one object; now two. The replay ring — recent
messages of every type, answering a BACKFILL from memory instead of waking
the SQL warehouse — moved to `job/stream.py`'s `RunStream`, which is built to
hold exactly that and nothing else (see its own module docstring for why:
the durable flusher now reads from it too, so its eviction rule has to be
stricter than a plain bounded ring). What is left here is not a leftover —
both remaining jobs are things `RunStream` has no way to know:

1. **Latest status.** One `StatusMessage`, replacing itself. The job reports
   this to Lakebase on every transition (`job/lakebase.py`), so a run's state
   is knowable whether or not any socket ever attached. The durable trace is
   separate and unconditional: every status message is also written to
   `run_events` on the normal durable path. `WebSocketBus` also reads this
   directly at teardown (`_force_terminal`) — the run's own outcome, kept
   without having to search anything for it.
2. **Progress history.** Bounded, kept for the end-of-run summary and for
   answering a client that missed the middle of a run. Deliberately NOT
   durability-gated the way `RunStream` is: this exists for `summary()` and
   whatever an end-of-run view wants, regardless of whether those same
   messages are still retained for replay.

Thread-safe: `observe()` is called from the model's worker thread through the
emitter.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any

from .shared.envelope import (
    TERMINAL_STATUSES,
    Message,
    ProgressMessage,
    RunStatus,
    StatusMessage,
    now_ms,
)

__all__ = ["RunRecord", "DEFAULT_PROGRESS_HISTORY"]

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
        progress_history: int = DEFAULT_PROGRESS_HISTORY,
    ) -> None:
        self.run_id = run_id
        self.model = model
        self.job_run_id = job_run_id
        self.started_ts = now_ms()

        self._lock = threading.Lock()
        self._progress: deque[ProgressMessage] = deque(maxlen=max(1, progress_history))
        self._status: StatusMessage | None = None
        self._latest_progress: ProgressMessage | None = None
        self._counts: dict[str, int] = {}

    # --- writing ----------------------------------------------------------

    def observe(self, msg: Message) -> None:
        """Every message the run produces passes through here, once."""
        with self._lock:
            self._counts[msg.type.value] = self._counts.get(msg.type.value, 0) + 1
            if isinstance(msg, StatusMessage):
                self._status = msg
            elif isinstance(msg, ProgressMessage):
                self._latest_progress = msg
                self._progress.append(msg)

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
        # Replay/durable-lag reporting moved with the ring -- see
        # `RunStream.describe()` for the counterpart this used to fold in.
        counts = self.counts()
        parts = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "nothing yet"
        return f"{self.run_id}: {self.status.value if self.status else 'no status'} ({parts})"
