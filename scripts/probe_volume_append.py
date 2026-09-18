#!/usr/bin/env python3
"""Can a job write its telemetry to a Unity Catalog volume, and how?

This is Slice 0 of docs/v4-rewrite-plan.md, and the only unknown that plan
rests on. v4 moves the durable path off Delta: the job writes each envelope
through to a file on `/Volumes/<catalog>/<schema>/telemetry/` as it is
produced. Everything downstream — no Spark in the harness, no in-memory
buffer, replay reading back its own log — assumes that works.

**Run this on a Databricks workspace, as a job**, not from a laptop. A laptop
writing to a local directory proves nothing about volume FUSE, which is the
thing in question. `--local` exists to develop and smoke-test the probe
itself, not to answer it.

    # on the workspace, as a job task
    python scripts/probe_volume_append.py --root /Volumes/main/dbx_leaning/telemetry

    # locally, to check the probe runs at all
    uv run python scripts/probe_volume_append.py --local --seconds 5

The question, precisely
-----------------------
Databricks volumes support sequential writes to new files. That is documented.
What is NOT documented, and what this decides, is whether a job can hold a
handle open across a long run and flush to it repeatedly — a much stronger
claim than "write a file once".

Two strategies, and the probe runs both:

  A. ONE FILE, HANDLE HELD OPEN. Open once, write, flush every few seconds,
     close at the end. Simplest possible durable path, and the nicest to
     replay from. Fails if FUSE does not support incremental flush to an open
     handle.

  B. ROLLING PART FILES. Write part-00001.jsonl, close it, open the next.
     Needs no append semantics whatsoever, and is the layout Auto Loader wants
     for ingestion anyway. Slightly more machinery, strictly more portable.

**B is the fallback and may simply be the answer.** A is worth probing because
if it works it is less code; nothing is lost if it does not.

What "works" has to mean
------------------------
Not "the write did not raise". This project has been burned twice by writes
that succeeded into the wrong place (delta-rs creating a local directory named
`main.dbx_leaning.run_logs`) or by values that were not what the schema said.
So each strategy is judged on four things, and a PASS needs all four:

  1. Writes do not raise.
  2. A SECOND reader, opening the path fresh mid-run, sees data already
     written. This is what makes it durable rather than buffered — and it is
     what replay and the ingestion job both depend on.
  3. The bytes read back are byte-identical to what went in.
  4. Nothing is lost across the whole run: every record written is present at
     the end, in order.

It also reports per-write latency, because that decides flush cadence. If a
single small write costs ~200 ms on FUSE, a flush-per-record design is dead
and the buffer this plan just deleted has to come back in some form — better
to learn that here than in Slice 1.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import statistics
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field


#: Written per tick, shaped like a real envelope so record size is honest
#: rather than a toy. A `log` message is the smallest thing that travels.
def _record(seq: int, run_id: str) -> dict:
    return {
        "type": "log",
        "run_id": run_id,
        "seq": seq,
        "ts": time.time_ns() // 1_000_000,
        "message": f"probe record {seq} — padding to a realistic width for a solver log line",
        "level": "INFO",
        "source": "probe",
        "phase": "run",
        "client_visible": True,
    }


@dataclass
class Result:
    strategy: str
    ok: bool = False
    writes: int = 0
    bytes_written: int = 0
    #: Records a second, independently-opened reader could see partway through.
    visible_midrun: int | None = None
    records_read_back: int = 0
    identical: bool = False
    in_order: bool = False
    latencies_ms: list[float] = field(default_factory=list)
    error: str | None = None

    @property
    def p50(self) -> float:
        return statistics.median(self.latencies_ms) if self.latencies_ms else 0.0

    @property
    def p99(self) -> float:
        if not self.latencies_ms:
            return 0.0
        ordered = sorted(self.latencies_ms)
        return ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))]

    def verdict(self) -> str:
        if self.error:
            return "FAIL"
        checks = (self.ok, bool(self.visible_midrun), self.identical, self.in_order)
        if all(checks):
            return "PASS"
        return "PARTIAL"


def _count_lines(path: pathlib.Path) -> int:
    """Open the path FRESH and count. Deliberately not reusing a handle — the
    point is what an independent reader (ingestion, replay) would see."""
    try:
        with open(path, encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())
    except FileNotFoundError:
        return 0


def probe_single_file(root: pathlib.Path, seconds: float, hz: float, run_id: str) -> Result:
    """Strategy A — one file, handle held open, flushed periodically."""
    res = Result(strategy="A: single file, handle held open")
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "part-00001.jsonl"

    written: list[dict] = []
    interval = 1.0 / hz
    deadline = time.monotonic() + seconds
    checked_midrun = False

    try:
        with open(path, "w", encoding="utf-8") as fh:
            seq = 0
            while time.monotonic() < deadline:
                rec = _record(seq, run_id)
                line = json.dumps(rec, separators=(",", ":")) + "\n"

                t0 = time.perf_counter()
                fh.write(line)
                fh.flush()
                # fsync is what actually pushes it through FUSE. If it is not
                # supported the write is buffered somewhere we cannot see, and
                # the mid-run visibility check below is what catches that.
                try:
                    os.fsync(fh.fileno())
                except OSError as exc:
                    res.error = f"fsync unsupported on this path: {exc}"
                res.latencies_ms.append((time.perf_counter() - t0) * 1000)

                written.append(rec)
                res.bytes_written += len(line.encode())
                seq += 1

                # Halfway through, ask whether anyone else can see this yet.
                if not checked_midrun and time.monotonic() > deadline - seconds / 2:
                    res.visible_midrun = _count_lines(path)
                    checked_midrun = True

                time.sleep(max(0.0, interval))
        res.writes = len(written)
        res.ok = True
    except Exception as exc:  # noqa: BLE001 — the probe's whole job is to catch this
        res.error = f"{type(exc).__name__}: {exc}"
        return res

    read_back = _read_all([path])
    res.records_read_back = len(read_back)
    res.identical = read_back == written
    res.in_order = [r["seq"] for r in read_back] == list(range(len(read_back)))
    return res


def probe_part_files(
    root: pathlib.Path, seconds: float, hz: float, run_id: str, per_part: int
) -> Result:
    """Strategy B — rolling part files, each opened, written, closed."""
    res = Result(strategy=f"B: rolling part files ({per_part} records each)")
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    written: list[dict] = []
    parts: list[pathlib.Path] = []
    interval = 1.0 / hz
    deadline = time.monotonic() + seconds
    checked_midrun = False
    seq = 0

    try:
        while time.monotonic() < deadline:
            batch = []
            for _ in range(per_part):
                if time.monotonic() >= deadline and batch:
                    break
                batch.append(_record(seq, run_id))
                seq += 1
                time.sleep(max(0.0, interval))
            if not batch:
                break

            path = run_dir / f"part-{len(parts) + 1:05d}.jsonl"
            body = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in batch)

            t0 = time.perf_counter()
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    # A closed file is durable on any sane filesystem; unlike
                    # strategy A this does not depend on fsync working.
                    pass
            res.latencies_ms.append((time.perf_counter() - t0) * 1000)

            parts.append(path)
            written.extend(batch)
            res.bytes_written += len(body.encode())

            if not checked_midrun and len(parts) >= 2:
                res.visible_midrun = sum(_count_lines(p) for p in parts[:-1])
                checked_midrun = True

        res.writes = len(parts)
        res.ok = True
    except Exception as exc:  # noqa: BLE001
        res.error = f"{type(exc).__name__}: {exc}"
        return res

    read_back = _read_all(sorted(parts))
    res.records_read_back = len(read_back)
    res.identical = read_back == written
    res.in_order = [r["seq"] for r in read_back] == list(range(len(read_back)))
    return res


def _read_all(paths: list[pathlib.Path]) -> list[dict]:
    out: list[dict] = []
    for p in paths:
        try:
            with open(p, encoding="utf-8") as fh:
                out.extend(json.loads(line) for line in fh if line.strip())
        except FileNotFoundError:
            continue
    return out


def probe_listing(root: pathlib.Path, run_id: str) -> tuple[bool, str]:
    """Can the run directory be listed? Ingestion and replay both need this,
    and FUSE listing is a separate capability from reading a known path."""
    run_dir = root / "runs" / run_id
    try:
        names = sorted(p.name for p in run_dir.iterdir())
        return True, f"{len(names)} entries: {', '.join(names[:4])}{'…' if len(names) > 4 else ''}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def _report(results: list[Result], listing: tuple[bool, str], root: pathlib.Path) -> int:
    print()
    print("=" * 78)
    print(f"VOLUME APPEND PROBE — {root}")
    print("=" * 78)

    for r in results:
        print(f"\n{r.verdict():>7}  {r.strategy}")
        if r.error:
            print(f"         error: {r.error}")
        print(f"         writes={r.writes} bytes={r.bytes_written:,}")
        print(
            f"         visible to a fresh reader mid-run: "
            f"{'—' if r.visible_midrun is None else r.visible_midrun}"
        )
        print(
            f"         read back={r.records_read_back} identical={r.identical} ordered={r.in_order}"
        )
        if r.latencies_ms:
            print(f"         write latency: p50={r.p50:.1f}ms p99={r.p99:.1f}ms")

    ok, detail = listing
    print(f"\n{'PASS' if ok else 'FAIL':>7}  directory listing")
    print(f"         {detail}")

    print("\n" + "-" * 78)
    a, b = results[0], results[1]
    if a.verdict() == "PASS":
        print("VERDICT: strategy A works — one file, handle held open. Use it; it is")
        print("         less code and replay reads one path. Keep B in mind if long")
        print("         runs behave differently from this probe's duration.")
    elif b.verdict() == "PASS":
        print("VERDICT: strategy A is NOT available; strategy B works. Adopt rolling")
        print("         part files — which is what Auto Loader wants anyway, so this")
        print("         is a fallback in name only.")
    else:
        print("VERDICT: NEITHER strategy passed. Do not start Slice 1. The v4 durable")
        print("         path assumes one of these works; if neither does, that")
        print("         assumption is wrong and the plan needs revisiting, not the")
        print("         code. Read the errors above before concluding anything.")
    print("-" * 78)
    print("\nPaste this output into docs/v4-rewrite-plan.md — Slice 0 is not done")
    print("until the answer is written down where the next session will read it.")

    return 0 if (a.verdict() == "PASS" or b.verdict() == "PASS") else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--root",
        default="/Volumes/main/dbx_leaning/telemetry",
        help="Volume root to probe (default: the telemetry volume).",
    )
    ap.add_argument(
        "--local",
        action="store_true",
        help="Write to a temp dir instead. Smoke-tests the probe; answers nothing.",
    )
    ap.add_argument("--seconds", type=float, default=60.0, help="How long to write for.")
    ap.add_argument("--hz", type=float, default=5.0, help="Records per second.")
    ap.add_argument("--per-part", type=int, default=25, help="Records per part file (strategy B).")
    ap.add_argument("--keep", action="store_true", help="Do not delete what was written.")
    args = ap.parse_args()

    if args.local:
        root = pathlib.Path(tempfile.mkdtemp(prefix="volume-probe-"))
        print(f"[--local] writing to {root} — this proves nothing about volume FUSE.")
    else:
        root = pathlib.Path(args.root)
        if not root.exists():
            print(f"ERROR: {root} does not exist or is not visible to this principal.")
            print("Apply uc_ddl/004_telemetry_volume.sql and check the job's grants:")
            print("  GRANT READ VOLUME, WRITE VOLUME ON VOLUME ... TO <job principal>")
            return 2

    run_id = f"probe-{int(time.time())}"
    half = args.seconds / 2
    print(f"run_id={run_id}  {args.seconds:.0f}s total, {args.hz:.0f} records/s\n")

    results = []
    try:
        print(f"strategy A: single file, handle open ({half:.0f}s)…")
        results.append(probe_single_file(root, half, args.hz, run_id + "-a"))
        print(f"strategy B: rolling part files ({half:.0f}s)…")
        results.append(probe_part_files(root, half, args.hz, run_id + "-b", args.per_part))
        listing = probe_listing(root, run_id + "-b")
        code = _report(results, listing, root)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 1
    finally:
        if not args.keep:
            for suffix in ("-a", "-b"):
                shutil.rmtree(root / "runs" / (run_id + suffix), ignore_errors=True)
            if args.local:
                shutil.rmtree(root, ignore_errors=True)

    return code


if __name__ == "__main__":
    sys.exit(main())
