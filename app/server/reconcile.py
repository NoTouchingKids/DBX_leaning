"""Startup reconciliation. Once, at startup. No background loop.

Apps run ~8h/day; jobs do not share that schedule. A run that started, or
finished, while this app was down is the normal case — so on the way up, any
run still marked non-terminal is checked against what the job itself recorded.

Three sources, asked in that order, cheapest and closest to the job first:

1. ``run_status_history`` in Lakebase — the job's own report of its own
   transition (``job/lakebase.py`` appends a row on every report). Postgres,
   so it costs no warehouse uptime at all.
2. ``run_events`` in Delta — the same transitions on the durable path, for a
   deploy with no Postgres, or a report that never reached it. Reading this
   wakes the SQL warehouse, which is the whole reason it is second now rather
   than first.
3. The Jobs API — the crash case, where the job never got to record an ending
   anywhere.

There should be far less for this to do than there once was: the job reports
its own status as it goes, so a run the app never observed usually arrives
here already terminal and never reaches the list at all.

There is deliberately no periodic version of this. A poll on a timer is what
keeps the SQL warehouse awake, and uptime is what costs money.
"""

from __future__ import annotations

import logging

from shared.envelope import TERMINAL_STATUSES, RunStatus

from .jobs_api import JobsApi
from .repository import RunRepository
from .store import RunStore

log = logging.getLogger(__name__)

__all__ = ["reconcile_once", "ReconcileReport"]

#: How far back into one run's transition log to look. A run has a handful of
#: transitions, not thousands — a bound against a pathological row, not a page
#: size. It is safe to be small because ``PostgresRunStore.history`` truncates
#: the OLDEST rows, and the only one read here is the newest.
_HISTORY_LOOKBACK = 50


class ReconcileReport:
    def __init__(self) -> None:
        self.checked = 0
        self.corrected: list[tuple[str, str]] = []
        self.still_running: list[str] = []
        self.errors: list[str] = []

    def __repr__(self) -> str:
        return (
            f"<reconciled checked={self.checked} corrected={len(self.corrected)} "
            f"still_running={len(self.still_running)} errors={len(self.errors)}>"
        )


async def reconcile_once(
    repo: RunRepository | None = None,
    jobs: JobsApi | None = None,
    store: RunStore | None = None,
) -> ReconcileReport:
    """``store`` is what this needs; ``repo`` and ``jobs`` are both optional
    sources of truth about a given run.

    ``repo`` became optional when ``run_status`` moved to Lakebase. The list
    of stale runs comes from the store, and the warehouse is only consulted
    for ``run_events`` — so a deploy with Postgres and no SQL warehouse can
    still reconcile, from the store's own transition log and the Jobs API,
    instead of never reconciling at all.
    """
    report = ReconcileReport()
    if store is None:
        report.errors.append("no run store; cannot reconcile")
        return report
    try:
        stale = [r.as_dict() for r in await store.non_terminal()]
    except Exception as exc:  # noqa: BLE001 - a degraded read path must not block startup
        report.errors.append(f"could not list non-terminal runs: {exc}")
        log.warning(report.errors[-1])
        return report

    for row in stale:
        run_id = row["run_id"]
        report.checked += 1
        try:
            resolved = await _resolve(store, repo, jobs, run_id, row.get("job_run_id"))
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"{run_id}: {exc}")
            continue

        if resolved is None:
            report.still_running.append(run_id)
            continue

        status, detail = resolved
        # No `ts=`, deliberately, and that is what makes this write unguarded:
        # `PostgresRunStore.set_status` orders a write that carries a message
        # timestamp against the row's, and every timestamp in play here comes
        # off another machine's clock — the job's `run_events` row, its
        # `run_status_history` row, the Jobs API. Hand one of those to the
        # guard and a second of clock skew is enough to have the correction
        # refused as stale; the finished run then keeps one of the account's
        # five task slots, and nothing tries again, because this pass is
        # startup-only. A correction is not part of the message stream and is
        # not ordered against it.
        await store.set_status(run_id, status, detail=detail)
        report.corrected.append((run_id, status.value))
        log.info("reconciled %s -> %s (%s)", run_id, status.value, detail)

    return report


async def _from_history(store: RunStore, run_id: str) -> tuple[RunStatus, str] | None:
    """Lakebase's own transition log — the cheapest source, so the first asked.

    ``run_status_history`` holds the same transitions ``run_events`` does, put
    there by the same writer (``job/lakebase.py`` appends one per report), but
    it is Postgres: reading it costs no warehouse uptime, and uptime rather
    than statement count is what this platform pays for. A run whose job
    reported its own ending while the app was down is resolved here without
    waking anything.

    ``history`` is deliberately NOT on the :class:`RunStore` Protocol — see
    ``store.py::PostgresRunStore.history`` — so this asks the store it was
    handed whether it has one. :class:`WarehouseRunStore` does not and is
    skipped, rather than being given a version returning ``[]`` that would
    make "this deploy has no history table" indistinguishable from "this run
    had no transitions".
    """
    read = getattr(store, "history", None)
    if not callable(read):
        return None

    try:
        transitions = await read(run_id, limit=_HISTORY_LOOKBACK)
    except Exception:  # noqa: BLE001 - the cheap source failing is not the end of the search
        # Fall through to the warehouse and the Jobs API rather than giving up
        # on this run: a history read that fails should cost a warehouse
        # wake-up, not a task slot stuck until someone edits the table.
        log.warning("could not read the transition history for %s", run_id, exc_info=True)
        return None

    if not transitions:
        return None

    # The NEWEST transition, and an answer only if it is terminal — NOT a scan
    # back through the list for the newest terminal one anywhere in it. A run
    # can legitimately go terminal and then non-terminal again: Databricks
    # retries a failed task, the retried attempt is handed the same ``run_id``,
    # and a FAILED is followed by a fresh RUNNING. Answering "FAILED" there
    # would mark a live run finished and hand back a task slot it is still
    # holding, which is the worse of the two errors available here. A
    # non-terminal newest row falls through to ``run_events`` and the Jobs API
    # instead, and they settle whether it really is still going.
    #
    # Stale reports do not corrupt this read the way they corrupt the
    # current-state row: ``history()`` orders by the transition's own ``ts``,
    # tie-broken by insertion order, not by when it happened to be recorded —
    # so a RUNNING redelivered and appended after a SUCCEEDED still reads
    # before it.
    try:
        status = RunStatus(transitions[-1].status)
    except ValueError:
        # An audit table keeps unrecognised values verbatim on purpose
        # (`StatusTransition` does not map one onto FAILED the way `RunRecord`
        # does). One is a data problem to look at, not a run's ending.
        return None
    if status not in TERMINAL_STATUSES:
        return None
    return status, "reconciled from run_status_history"


async def _resolve(
    store: RunStore,
    repo: RunRepository | None,
    jobs: JobsApi | None,
    run_id: str,
    job_run_id: str | None,
) -> tuple[RunStatus, str] | None:
    """The job's own record first — twice — and the Jobs API last.

    ``run_status_history`` and ``run_events`` are both what the job wrote as
    it went, so both are closer to the truth than anything the app inferred.
    The history is asked first only because it is free; the warehouse read
    behind it is what answers when Lakebase is not configured or the job's
    REST report never landed. The Jobs API gets asked about runs the job never
    got to finish recording anywhere — the crash case.

    With neither a history nor a ``repo`` — no Postgres and no SQL warehouse —
    the Jobs API is the only source, which is weaker but is the difference
    between reconciling and not.
    """
    from_history = await _from_history(store, run_id)
    if from_history is not None:
        return from_history

    event = await repo.latest_event(run_id) if repo is not None else None
    if event:
        try:
            status = RunStatus(event["status"])
        except ValueError:
            status = None
        if status is not None and status in TERMINAL_STATUSES:
            return status, "reconciled from run_events"

    if jobs is None or not job_run_id:
        return None

    run = await jobs.get_run(job_run_id)
    if run is None:
        return None
    terminal = jobs.terminal_status(run)
    if terminal is None:
        return None
    return RunStatus(terminal), f"reconciled from jobs api (job_run_id={job_run_id})"
