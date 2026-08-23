"""Startup reconciliation. Once, at startup. No background loop.

Apps run ~8h/day; jobs do not share that schedule. A run that started, or
finished, while this app was down is the normal case — so on the way up, any
run still marked non-terminal is checked against what the job itself recorded
in ``run_events`` and, failing that, against the Jobs API.

There is deliberately no periodic version of this. A poll on a timer is what
keeps the SQL warehouse awake, and uptime is what costs money.
"""

from __future__ import annotations

import logging

from shared.envelope import TERMINAL_STATUSES, RunStatus

from .jobs_api import JobsApi
from .repository import RunRepository

log = logging.getLogger(__name__)

__all__ = ["reconcile_once", "ReconcileReport"]


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


async def reconcile_once(repo: RunRepository, jobs: JobsApi | None = None) -> ReconcileReport:
    report = ReconcileReport()
    try:
        stale = await repo.non_terminal_runs()
    except Exception as exc:  # noqa: BLE001 - a degraded read path must not block startup
        report.errors.append(f"could not list non-terminal runs: {exc}")
        log.warning(report.errors[-1])
        return report

    for row in stale:
        run_id = row["run_id"]
        report.checked += 1
        try:
            resolved = await _resolve(repo, jobs, run_id, row.get("job_run_id"))
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"{run_id}: {exc}")
            continue

        if resolved is None:
            report.still_running.append(run_id)
            continue

        status, detail = resolved
        await repo.set_run_status(run_id, status.value, detail=detail)
        report.corrected.append((run_id, status.value))
        log.info("reconciled %s -> %s (%s)", run_id, status.value, detail)

    return report


async def _resolve(
    repo: RunRepository, jobs: JobsApi | None, run_id: str, job_run_id: str | None
) -> tuple[RunStatus, str] | None:
    """The job's own record first, the Jobs API second.

    ``run_events`` is what the job wrote durably as it went, so it is closer to
    the truth than anything the app inferred. The Jobs API only gets asked
    about runs the job never got to finish recording — the crash case.
    """
    event = await repo.latest_event(run_id)
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
