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
2. **`replay()` reads three sources, not one.** Closed parts alone would
   silently omit the newest records — exactly the ones a client that just
   reconnected is missing. Closed parts plus `_pending` is the next-obvious
   answer and it is *also* incomplete: a roll in progress has already taken its
   batch out of `_pending` and its file is not readable yet, so for the ~117ms
   a close costs those records are in neither. `_inflight` is the third source
   and it closes that window. The union of the three is precisely the set of
   records this run has issued, which is the property replay is supposed to
   have and did not.

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
from typing import Any, Protocol

log = logging.getLogger(__name__)

__all__ = ["PartFileWriter", "TelemetryWriter"]

#: Roll when the pending part reaches this many bytes. A part is one file and
#: one close, so this trades file count against how much a crash can lose.
DEFAULT_MAX_BYTES = 1_000_000

#: Roll when the oldest pending record is this old. **This is the bound on
#: data loss**, and with a volume it is exact rather than approximate: nothing
#: written since the last close survives the process dying.
DEFAULT_MAX_AGE_S = 30.0


class TelemetryWriter(Protocol):
    """What the harness needs from a durable path. Satisfied by
    `PartFileWriter`; the interface exists so tests can substitute something
    that does not touch a filesystem, not because a second one is planned.

    A `Protocol` rather than a base class, and structurally so: `PartFileWriter`
    does not inherit it, which is the point — a substitute has to match the
    shape, not the ancestry.
    """

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

        #: Batches that have LEFT `_pending` and whose part file has not
        #: finished being written, keyed by part number.
        #:
        #: This exists for `replay()` and for nothing else. `_roll` takes a
        #: batch out of `_pending` under the lock and then spends ~117ms
        #: (p99 157ms, Slice 0) inside `write_text` — during which the file is
        #: not yet readable, because a UC volume materialises a file only on
        #: close. Without this dict those records are in NEITHER half of the
        #: union replay reads, so a replay landing in that window silently
        #: returns a hole: measured here as replay(0) returning [] for a run
        #: that had issued five records.
        #:
        #: A dict rather than one list because two threads roll — the roller
        #: on age/size and `close()` at end of run — and each must be able to
        #: have a batch in flight without clobbering the other's.
        #:
        #: **The invariant every read here depends on: a record is always in
        #: at least one of `_pending`, `_inflight`, or a closed part file.**
        #: Every transition between the three happens inside one critical
        #: section, so there is no instant at which a record is in none of
        #: them. It may briefly be in two, which is why `replay` dedupes.
        self._inflight: dict[int, list[dict[str, Any]]] = {}

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
        would lose right now, and what makes a SUCCEEDED claim dishonest.

        `_inflight` is deliberately NOT counted, and the omission is not an
        oversight. A batch in flight is one whose `write_text` has not returned;
        counting it would make this number flicker non-zero for ~117ms on every
        roll, and the one caller that acts on it — `Harness._finalise`, which
        turns SUCCEEDED into FAILED — runs after `close()` has completed, so it
        would be reading a value that says "lost" about a write that is about
        to succeed.
        """
        with self._lock:
            return len(self._pending)

    def replay(self, from_seq: int, to_seq: int | None = None) -> list[dict[str, Any]]:
        """Records with `from_seq <= seq <= to_seq`. Both ends INCLUSIVE.

        Inclusive at both ends because that is what the caller asks for: the
        client computes a gap as `lastSeq + 1 .. thisSeq - 1`, which is the
        set of seqs it is missing, not a half-open interval around them.

        THREE sources, not two. Reading only the closed parts is the obvious
        implementation and it is wrong in the one case replay exists for — a
        client that just reconnected is missing the *newest* records, which are
        the ones still pending. Reading parts + pending is the next-obvious one
        and it is *also* wrong, for a narrower and nastier reason: a roll
        in progress has already taken its batch out of `_pending` and its file
        is not yet readable, so for the ~117ms a UC volume close costs those
        records exist in neither. That is a silent hole in a gap fill, which is
        worse than an error, because the client hides the gap banner either
        way. `_inflight` is the third source and closes it.

        Two orderings are load-bearing here and both are cheap to get wrong:

        1. **Snapshot the in-memory halves first, read the files second.** A
           roll landing between the two then shows a record TWICE, which the
           dedupe below absorbs. The other order shows it ZERO times, which
           nothing downstream can detect or repair.
        2. **`_pending` and `_inflight` are snapshotted in ONE critical
           section.** Taking them separately reopens the same hole a roll can
           slip through between the two acquisitions.

        Deduping by `seq` is exact rather than approximate: `SeqCounter` hands
        each value out once per run and every emitted message consumes exactly
        one, so two records with the same seq ARE the same record.
        """
        with self._lock:
            held = list(self._pending)
            for batch in self._inflight.values():
                held.extend(batch)

        by_seq: dict[int, dict[str, Any]] = {}
        for record in held:
            if _in_range(record, from_seq, to_seq):
                by_seq.setdefault(record["seq"], record)
        for record in self._read_parts():
            if _in_range(record, from_seq, to_seq):
                by_seq.setdefault(record["seq"], record)

        # Sorted by the seq itself, so the order a part file happened to be
        # read in cannot leak into the answer.
        return [by_seq[seq] for seq in sorted(by_seq)]

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
            # In the SAME critical section that empties `_pending`. Anything
            # in between would leave these records in neither place, and
            # `replay` would answer with a hole for as long as the write takes.
            self._inflight[part_no] = batch

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
                self._inflight.pop(part_no, None)
                self._pending = batch + self._pending
                self._pending_bytes += len(body)
                if self._oldest_ts is None:
                    self._oldest_ts = time.monotonic()
            return False

        # AFTER the write returns, never before. Until then the file is not
        # readable — on a UC volume it does not exist to another reader at all
        # — so dropping the in-flight copy any earlier recreates exactly the
        # window this dict was added to close. Between the write returning and
        # this line a record is visible twice; `replay` dedupes by seq.
        with self._lock:
            self._inflight.pop(part_no, None)

        self.rows_written += len(batch)
        self.parts_written += 1
        return True

    def _read_parts(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        # `sorted` here is LEXICOGRAPHIC, and safe twice over: `part-%05d`
        # zero-pads, so lexicographic and numeric agree up to part-99999, and
        # `replay` sorts its answer by `seq` regardless, so the order files are
        # read in never reaches a caller. Dropping the padding would quietly
        # break the first of those and leave the second holding it up.
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
