"""The run-status writer slot: the interface, and the no-op it defaults to.

`run_status` lives in Lakebase (Postgres) — one row per run, upserted on every
transition — and the harness is its writer (docs/v5-implementation-plan.md,
Phase 2). The writer itself is `job/lakebase.py::LakebaseStatusWriter`; this
module is the seam it plugs into, so the harness does not import a Postgres
driver and can be exercised with any object of the right shape.

The contract, all of it:

* ``write(run_id, seq, status, terminal, detail, ts)`` is **called once for
  every `status` message the run emits** — the harness's own RUNNING and
  terminal status, and any status a model emits along the way — with that
  message's own fields, `ts` in epoch ms. `seq` is the clock to guard the
  upsert with, never `now()`: a late write always carries the later `now()`,
  which makes a `now()` guard inert on exactly the path that needs it. The
  return value is advisory: False (or a raise) is counted as a failure.
* **It must never raise into the harness.** The harness catches anyway, but a
  writer that relies on that is a writer whose failures are logged as the
  harness's. Count instead (`writes` / `failures` / `last_error`), so a
  best-effort path is observable without being load-bearing.
* **It must bound its own calls** (connect and statement timeouts). The
  harness can bound how long it WAITS for a write, never how long the write
  takes — a thread cannot be preempted.
* ``close()``, if the writer has one, is called once at the end of the run,
  after the terminal write. Optional; idempotent is expected.

A `Protocol`, so a writer matches by shape rather than by ancestry — the same
rule models follow. `close` is not in it because it is optional.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = ["NullStatusWriter", "StatusWriter"]


@runtime_checkable
class StatusWriter(Protocol):
    """Where a run's status transitions go besides the part files."""

    def write(
        self,
        run_id: str,
        seq: int,
        status: str,
        terminal: bool,
        detail: str | None,
        ts: int,
    ) -> Any: ...


class NullStatusWriter:
    """The default: nothing configured, nothing written, nothing to fail.

    A run with no Lakebase is not degraded — the part files carry every status
    and the app backfills from them. The harness recognises this class and
    skips the status path entirely, so a run without a writer does exactly the
    work it did before the slot existed.
    """

    def write(
        self,
        run_id: str,
        seq: int,
        status: str,
        terminal: bool,
        detail: str | None,
        ts: int,
    ) -> bool:
        return True

    def close(self) -> None:
        return None
