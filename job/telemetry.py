"""The durable path: a run's telemetry, written as rolling part files.

One directory per run on the telemetry volume, one JSONL file per part:

    /Volumes/<catalog>/<schema>/telemetry/runs/<run_id>/part-00001.jsonl

**Slice 0 decided this shape and the reason matters more than the shape.**
Holding a single file handle open and flushing to it does not work on a Unity
Catalog volume: FUSE buffers the whole file locally and materialises it on
close, so a second reader sees nothing until then — and `write`, `flush` and
`fsync` all return success the entire time, at ~0.3ms, which is memory speed.
Measured 2026-09-01 (`scripts/probe_volume_append.py`).

Two consequences run through everything here:

1. **A part is durable when it is CLOSED, and not before.** So "write through,
   no buffer" is not achievable — records accumulate in `_pending` until a roll
   closes a file over them. What v3's flush policy did, a roll does; it is the
   same rule with a cheaper flush. `unflushed` is how the harness refuses to
   report SUCCEEDED over a lost write.
2. **`replay()` must read `_pending` too.** Closed parts alone would silently
   omit the newest records — exactly the ones a client that just reconnected is
   missing. This is not extra machinery: the buffer exists in order to be
   written, and its union with the closed parts is precisely the set of records
   this run has issued.

A close costs ~117ms (p99 157ms), which makes the roll cadence a priced trade
rather than a free dial: at 5 records/s, rolling every 5s spends ~2.3% of
wall-clock closing files, and every 500ms would spend ~23%. Do not lower
`max_age_s` for "better durability" without pricing it.

Nothing here imports Spark, Delta or Unity Catalog. It writes files to a path.
That is what makes the harness portable, and it is why `job/delta.py` is gone.
"""

from __future__ import annotations

import json
import logging
import pathlib
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

__all__ = ["PartFileWriter", "TelemetryWriter"]

#: Roll when the pending part reaches this many bytes. A part is one file and
#: one close, so this trades file count against how much a crash can lose.
DEFAULT_MAX_BYTES = 1_000_000

#: Roll when the oldest pending record is this old. **This is the bound on
#: data loss**, and with a volume it is exact rather than approximate: nothing
#: written since the last close survives the process dying.
DEFAULT_MAX_AGE_S = 30.0


class TelemetryWriter:
    """What the harness needs from a durable path. Implemented by
    `PartFileWriter`; the interface exists so tests can substitute something
    that does not touch a filesystem, not because a second one is planned."""

    def append(self, record: dict[str, Any]) -> None: ...
    def roll_if_due(self) -> bool: ...
    def close(self) -> None: ...
    def replay(self, from_seq: int, to_seq: int | None = None) -> list[dict[str, Any]]: ...


class PartFileWriter:
    """Rolling JSONL part files under one run's directory.

    Thread-safe: the model thread appends while the harness's roller thread
    ages parts out, so every mutation of `_pending` is under `_lock`. The
    actual file write happens outside the lock — a ~117ms close must not block
    a model mid-solve.
    """

    def __init__(
        self,
        root: str | pathlib.Path,
        run_id: str,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_age_s: float = DEFAULT_MAX_AGE_S,
    ) -> None:
        self.run_dir = pathlib.Path(root) / "runs" / run_id
        self.max_bytes = max_bytes
        self.max_age_s = max_age_s

        self._lock = threading.Lock()
        self._pending: list[dict[str, Any]] = []
        self._pending_bytes = 0
        self._oldest_ts: float | None = None
        self._part_no = 0

        #: Records closed into a part file. The harness compares this against
        #: what was appended to decide whether SUCCEEDED is honest.
        self.rows_written = 0
        self.parts_written = 0
        self.write_failures = 0
        self.last_error: str | None = None

        self.run_dir.mkdir(parents=True, exist_ok=True)

    # --- what the harness calls -------------------------------------------

    def append(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._pending.append(record)
            # Approximate, and deliberately: exact sizing means serialising
            # twice, and this only decides when to roll.
            self._pending_bytes += len(json.dumps(record, separators=(",", ":")))
            if self._oldest_ts is None:
                self._oldest_ts = time.monotonic()

    def roll_if_due(self) -> bool:
        """Roll on size OR age. Returns whether a part was written."""
        with self._lock:
            if not self._pending:
                return False
            aged = self._oldest_ts is not None and (
                time.monotonic() - self._oldest_ts >= self.max_age_s
            )
            if not aged and self._pending_bytes < self.max_bytes:
                return False
        return self._roll()

    def close(self) -> None:
        """End of run: roll whatever is left, whatever the outcome was.

        Called from a `finally`, so it must not raise — a failure here is
        recorded in `unflushed`/`last_error` and answered by the harness
        refusing to call the run SUCCEEDED, which is more useful than an
        exception thrown over whatever already went wrong.
        """
        self._roll()

    @property
    def unflushed(self) -> int:
        """Records appended but not yet closed into a part — i.e. what a crash
        would lose right now, and what makes a SUCCEEDED claim dishonest."""
        with self._lock:
            return len(self._pending)

    def replay(self, from_seq: int, to_seq: int | None = None) -> list[dict[str, Any]]:
        """Records with `from_seq <= seq <= to_seq`, from parts AND pending.

        Both halves, always. Reading only the closed parts is the obvious
        implementation and it is wrong in the one case replay exists for: a
        client that just reconnected is missing the *newest* records, and those
        are precisely the ones still pending. See this module's header.
        """
        with self._lock:
            pending = list(self._pending)

        found = [r for r in self._read_parts() if _in_range(r, from_seq, to_seq)]
        found.extend(r for r in pending if _in_range(r, from_seq, to_seq))
        found.sort(key=lambda r: r.get("seq", 0))
        return found

    # --- internals --------------------------------------------------------

    def _roll(self) -> bool:
        with self._lock:
            if not self._pending:
                return False
            batch = self._pending
            self._pending = []
            self._pending_bytes = 0
            self._oldest_ts = None
            self._part_no += 1
            part_no = self._part_no

        path = self.run_dir / f"part-{part_no:05d}.jsonl"
        body = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in batch)
        try:
            # Written whole and closed. No handle is held across records —
            # that is the thing Slice 0 showed does not become durable.
            path.write_text(body, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - a lost write is an outcome
            self.write_failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.error("could not write %s: %s", path, exc)
            # Put them back: unwritten records are still this run's to account
            # for, and `unflushed` is what stops SUCCEEDED being claimed over
            # them. A later roll may well succeed.
            with self._lock:
                self._pending = batch + self._pending
                self._pending_bytes += len(body)
                if self._oldest_ts is None:
                    self._oldest_ts = time.monotonic()
            return False

        self.rows_written += len(batch)
        self.parts_written += 1
        return True

    def _read_parts(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for path in sorted(self.run_dir.glob("part-*.jsonl")):
            try:
                with open(path, encoding="utf-8") as fh:
                    out.extend(json.loads(line) for line in fh if line.strip())
            except (OSError, json.JSONDecodeError) as exc:
                # One unreadable part must not lose the rest: replay is
                # best-effort by nature, and the durable record is still there
                # for the ingestion job to pick up.
                log.warning("skipping unreadable part %s: %s", path, exc)
        return out


def _in_range(record: dict[str, Any], from_seq: int, to_seq: int | None) -> bool:
    seq = record.get("seq")
    if not isinstance(seq, int) or seq < from_seq:
        return False
    return to_seq is None or seq <= to_seq
