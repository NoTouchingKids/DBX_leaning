"""The orphan sweep: finds a run whose job died without telling anyone.

A job that dies hard — OOM, SIGKILL, the cluster underneath it disappearing —
never runs its own teardown. It sends no terminal `status` frame over the
WebSocket, and it never reaches the line in `job/lakebase.py` that would have
upserted `run_status` and appended `run_status_history`. Nothing downstream
was lied to; nothing was told anything at all. `run_status` just sits on
whatever it last said — usually `RUNNING` — forever.

That is not merely a stale row. `PostgresRunStore.claim_slot` counts
non-terminal rows against Free Edition's account-wide ceiling of 5 concurrent
job tasks, so one crashed run keeps costing 20% of the platform's entire
concurrency budget until something corrects it. Startup reconciliation
(`reconcile.py::reconcile_once`) is that something, but it runs once, at
startup — and this app runs ~8h/day. A run that dies at 10am does not get
corrected until tomorrow's restart, which is most of a working day spent at
80% capacity for no reason visible to anyone.

This module is what closes that gap without opening the one startup
reconciliation deliberately avoids: it never touches the SQL warehouse.
`sweep_once` reuses `reconcile.resolve_ending` with `repo=None`, which gets
Lakebase's own transition history for free (Postgres, no warehouse involved)
but skips `run_events` outright, because that branch needs a `repo` it is
never given. What is left standing is exactly the Jobs API: "is this task
still alive, according to Databricks" — a REST call, not a SQL statement, and
the one source a crashed job cannot have faked or suppressed on its way down.

Two things stand between "non-terminal" and "orphaned", and getting them
wrong in either direction is a real failure, not a cosmetic one:

- A run with a live WebSocket is not a candidate at all, by definition —
  something is actively talking to it right now.
- A run without one might just be mid-reconnect (the job redials on a timer,
  `DBX_WS_RECONNECT_S` in `job/config.py`) or might not have attached yet (a
  serverless task takes tens of seconds to start). Both are ordinary, and
  both are why this waits out a grace period — see `config.py`,
  `orphan_sweep_min_age_s` and `orphan_sweep_socket_grace_s` — before it
  trusts a missing socket as evidence of anything.

Even inside that grace window, this never *concludes* a run is dead on its
own say-so: `resolve_ending` always asks the Jobs API, and only a definite
terminal answer from Databricks produces a correction. "Could not find out"
(no Jobs API configured, an unreachable one, an exception) always means
"leave it alone, try again next tick" — never "assume the worst".

There is one narrower gap even that leaves standing, and it is what
`_never_launched` below exists for. `resolve_ending` skips its Jobs API step
outright when a run has no `job_run_id` — there is no Databricks run to ask
about — and normally that costs nothing, because a job that actually started
reports its own status straight to Lakebase (`job/lakebase.py`) independently
of whether the app ever recorded its `job_run_id`, so `_from_history` picks
it up regardless, and usually `job_run_id` itself arrives that way too (the
job knows its own id from its own environment even when the app never got to
attach it — see `job/lakebase.py::REPORT_SQL`'s `COALESCE` on that column).
What survives both of those is `routes/runs.py::trigger_run` itself dying
between `claim_slot` (which writes the row, deliberately first — see that
function's own docstring) and `run_now`/`attach_job_run` completing: no job
was ever launched, so nothing is ever going to report anything, to Lakebase
or anywhere else, about this `run_id`. Left alone, that row holds one of the
account's five task slots forever — no restart and no amount of waiting ever
produces an answer, which is worse than any case `resolve_ending` itself
handles: a crashed job at least *had* a `job_run_id` to ask about.

`_never_launched` is the one place in this module — and the only place in
either sweep — that writes a terminal status with no live source left to
confirm it against. That makes it the one path here that could end a run
that is genuinely still in flight if it is ever too eager, so it is guarded
twice, and both guards are required: a much larger age bound than anything
else in this file (`OrphanSweeper` derives it from `min_age_s` itself rather
than a fixed constant of its own, so it cannot be tuned close by accident —
see `_NEVER_LAUNCHED_AGE_MULTIPLIER` below), and a check that
`run_status_history` has *nothing at all* for the run, not merely nothing
terminal. Any row there — even a lone `RUNNING` — is proof the job got as
far as reporting something, and vetoes the release outright; what happens to
that run next is `resolve_ending`'s question to answer on some later tick,
not this one's.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Protocol, runtime_checkable

from shared.envelope import RunStatus, now_ms

from .jobs_api import JobsApi
from .reconcile import resolve_ending
from .store import RunRecord, RunStore

log = logging.getLogger(__name__)

__all__ = ["OrphanSweeper", "SweepReport", "sweep_once"]


@runtime_checkable
class SocketRegistry(Protocol):
    """The one fact this needs from `services.JobConnections`.

    Named apart, structurally, rather than imported from `services.py`:
    `services.py` builds and owns the `OrphanSweeper` (so it can start it in
    `ServiceHub.startup` and stop it in `ServiceHub.shutdown`), and a module
    that both constructs something and is imported by the thing it
    constructs is a cycle. A one-method Protocol costs nothing and reads
    exactly like what it is used for.
    """

    def is_connected(self, run_id: str) -> bool: ...


class SweepReport:
    """One tick's outcome. Logged, not returned to an HTTP caller — nothing
    user-facing waits on a background sweep — but kept as a small object
    rather than folded straight into log calls, for the same reason
    `reconcile.ReconcileReport` is: an assertion in a test reads better than
    a regex over a log line.
    """

    def __init__(self) -> None:
        self.checked = 0
        self.corrected: list[tuple[str, str]] = []
        self.skipped_live = 0
        #: Too young (`orphan_sweep_min_age_s`) or too recently updated
        #: (`orphan_sweep_socket_grace_s`) — kept as one counter because both
        #: mean the same thing to a caller: "not a candidate yet, and that is
        #: fine." Which rule applied is in the debug log, not here.
        self.skipped_too_recent = 0
        self.errors: list[str] = []

    def __repr__(self) -> str:
        return (
            f"<orphan sweep checked={self.checked} corrected={len(self.corrected)} "
            f"skipped_live={self.skipped_live} skipped_too_recent={self.skipped_too_recent} "
            f"errors={len(self.errors)}>"
        )


#: How large a multiple of `min_age_s` a claim with no `job_run_id` must
#: clear before its silence is trusted as "never launched" rather than
#: "still starting" — see `_never_launched` and the module docstring's
#: closing section. An order of magnitude, not a close multiple: `min_age_s`
#: only has to outlast a serverless cold start (tens of seconds); this also
#: has to outlast `trigger_run` itself still being legitimately in flight
#: between `claim_slot` and `run_now`/`attach_job_run` returning, which is
#: bounded by a couple of HTTP calls, not by anything measured in minutes.
#: Getting this too tight risks the one mistake this module cannot take back.
_NEVER_LAUNCHED_AGE_MULTIPLIER = 10

#: Absolute floor under the derived value above. `min_age_s` is tuned to 0 in
#: plenty of this file's own tests, and a real deployment might reasonably
#: tune it low too, to catch an ordinary crashed-with-a-`job_run_id` run
#: sooner — that must not also drag the never-launched bound down to
#: something a merely slow `trigger_run` could still clear. 30 minutes is
#: generous next to both an HTTP call and a serverless cold start, and still
#: same-business-day at the default `orphan_sweep_interval_s`.
_NEVER_LAUNCHED_AGE_FLOOR_S = 1800.0


async def _reported_anything(store: RunStore, run_id: str) -> bool | None:
    """Whether `run_status_history` has so much as one row for this run.

    A different question from `reconcile._from_history`'s: that one asks
    whether the *newest* transition is terminal, to resolve a run that ended
    quietly. This asks whether there is anything at all, to tell "the job ran
    and this app just does not know how it ended" apart from "the job never
    ran". `job/lakebase.py::REPORT_SQL` appends a history row in the same
    statement as the `run_status` upsert on every single report the job makes
    — terminal or not, and even on the rare report whose upsert half is a
    no-op under the guard, per that module's own docstring — so a row with
    zero history is not "hasn't reached a terminal state yet"; it is "has
    never made a single report of any kind".

    Returns `None`, not `False`, when the question cannot be answered at all
    — no `history()` on this store (see `store.py::PostgresRunStore.history`
    — deliberately not on the `RunStore` Protocol, per `reconcile.py`'s own
    note), or the read itself raising. The caller must treat that the same
    way `resolve_ending` treats a Jobs API that could not answer: "leave it
    alone", never "assume the worst" — an unreadable answer is not evidence
    of anything.
    """
    read = getattr(store, "history", None)
    if not callable(read):
        return None
    try:
        transitions = await read(run_id, limit=1)
    except Exception:  # noqa: BLE001 - an unreadable answer must not be read as "no"
        log.warning(
            "orphan sweep: could not read run_status_history for %s while checking "
            "whether it was ever launched",
            run_id,
            exc_info=True,
        )
        return None
    return bool(transitions)


async def _never_launched(
    store: RunStore, record: RunRecord, *, now: int, never_launched_age_ms: float
) -> tuple[RunStatus, str] | None:
    """The one resolution in this module that asks Databricks nothing.

    Only reached from `sweep_once` when `resolve_ending` has already given up
    on a candidate AND that candidate has no `job_run_id` — meaning
    `resolve_ending` never had a Jobs API question to ask in the first place
    (see its own docstring). What is left is a narrower one: not "how did it
    end", but "did it ever begin". `routes/runs.py::trigger_run` writes this
    row (`claim_slot`) strictly before it calls `run_now`, so a row old
    enough, with no `job_run_id` and no history of any kind, is consistent
    with exactly one story: the app that claimed the slot died — between
    `claim_slot` and `run_now`, or in the narrow window right after, before
    either its own `attach_job_run` or the job's own first report could land
    anywhere. See the module docstring's closing section for the fuller case.

    Two independent guards, both required, because this is the one path here
    that could end a run that is genuinely still in flight if either is
    wrong:

    - `never_launched_age_ms` — generous by construction
      (`OrphanSweeper.__init__` derives it from `min_age_s`, not a constant
      of its own) — rules out `trigger_run` itself still legitimately running.
    - `_reported_anything` — rules out the job having started regardless of
      what this row's own columns show: once the job has made even one
      report, `job_run_id` usually arrives with it (see the module
      docstring), so this branch should be rare in practice; when it is not,
      `_reported_anything` is what stands between "definitely never
      launched" and "launched, then went quiet before saying anything at
      all", and only the former may be released here.
    """
    if now - record.started_ts < never_launched_age_ms:
        return None

    reported = await _reported_anything(store, record.run_id)
    if reported is not False:
        # True: something is in the history despite no `job_run_id` on this
        # row — proof it launched, whatever happens to it next. None: could
        # not be read, which is "cannot tell", not "confirmed empty". Either
        # way, this is not the never-launched case.
        return None

    age_s = int((now - record.started_ts) / 1000)
    detail = (
        "orphan sweep: never launched — no job_run_id was ever attached to this run and "
        f"run_status_history has no record of it either, {age_s}s after it claimed a slot; "
        "inferred from the absence of a job run id, not reported by a job that ran and failed "
        "(see routes/runs.py::trigger_run and the orphan_sweep module docstring)"
    )
    # QUEUED at claim time and never reported again — read deliberately as
    # CANCELLED, not FAILED: nothing was ever observed to go wrong (no
    # exception, no Databricks error, not even a Databricks run to have
    # failed). This is the app abandoning a claim it can never verify the
    # outcome of, not a report that the model or the job misbehaved. The
    # frontend already encodes exactly that distinction — every per-model
    # signature view (`app/client/src/components/models/*/`) paints FAILED in
    # the alarming "bad" tone and CANCELLED in the neutral, dimmed "idle" one
    # (e.g. `mcmc/McmcSignature.tsx`, `forecasting/ForecastingSignature.tsx`)
    # — so this is the "nothing to investigate" bucket, and FAILED would be a
    # false alarm sending someone to look for job logs that were never
    # written, for a Databricks run that never existed.
    return RunStatus.CANCELLED, detail


async def sweep_once(
    store: RunStore | None,
    job_sockets: SocketRegistry,
    jobs: JobsApi | None,
    *,
    min_age_s: float,
    socket_grace_s: float,
    never_launched_age_s: float,
) -> SweepReport:
    """One tick. Never raises: every dependency this touches is optional and
    every failure is caught here rather than left to whoever is looping this
    (`OrphanSweeper._loop`) — a sweep that dies on its first bad response is
    worse than no sweep, because nothing is left running to say so.
    """
    report = SweepReport()

    if store is None:
        log.debug("orphan sweep: no run store configured; nothing to check this tick")
        return report

    # `store.non_terminal()` is the one call in this whole module that could
    # ever reach the SQL warehouse, and it does exactly that on
    # `WarehouseRunStore` — `repository.non_terminal_runs` is a SELECT over
    # Delta. Calling it on a timer would be the precise cost mistake this
    # platform exists to avoid, just relocated from a status poll to an
    # orphan sweep. So this store is skipped outright rather than swept
    # degraded: a deploy without Lakebase gets no periodic protection here,
    # and keeps exactly what it already had — startup reconciliation only,
    # same as before this module existed. `PostgresRunStore.non_terminal` has
    # no such cost (a point read against a partial index), which is the only
    # reason any store gets to run this on a schedule at all.
    if store.name != "postgres":
        log.debug(
            "orphan sweep: run store is %r, not Lakebase; skipping so this tick "
            "never wakes the SQL warehouse on a timer (see module docstring)",
            store.name,
        )
        return report

    if jobs is None:
        # The only source this asks that is not Lakebase. Without it there is
        # nothing left to confirm a death with — a missing socket alone is
        # not evidence, per the module docstring — so there is nothing to do
        # until it is configured.
        log.debug("orphan sweep: no Jobs API configured; nothing to check a socketless run against")
        return report

    try:
        candidates = await store.non_terminal()
    except Exception as exc:  # noqa: BLE001 - a degraded read must not end the loop
        report.errors.append(f"could not list non-terminal runs: {exc}")
        log.warning("orphan sweep: %s", report.errors[-1], exc_info=True)
        return report

    now = now_ms()
    min_age_ms = min_age_s * 1000
    socket_grace_ms = socket_grace_s * 1000
    never_launched_age_ms = never_launched_age_s * 1000

    for record in candidates:
        if job_sockets.is_connected(record.run_id):
            # Live by definition: whatever else might be true, something is
            # actively attached to it right now.
            report.skipped_live += 1
            continue

        if now - record.started_ts < min_age_ms:
            # `run-now` returns before a serverless task has even started, so
            # a row seconds old with nothing attached yet is the ordinary
            # shape of "just launched" — not a run that died.
            report.skipped_too_recent += 1
            continue

        if now - record.updated_ts < socket_grace_ms:
            # Nothing has moved on this row recently enough to trust a
            # missing socket as anything but the job's own reconnect timer
            # doing its job. Asking the Jobs API this early would usually
            # just come back "still running" — cheap, but pointless — and
            # skipping costs nothing: this tick, or the next one, will look
            # at it again as soon as it clears the grace period.
            report.skipped_too_recent += 1
            continue

        report.checked += 1
        try:
            # `repo=None`: see the module docstring. This still gets a look
            # at Lakebase's own history for nothing — cheap, and it is what
            # lets a run resolve here even in the rare case the job managed
            # to write `run_status_history` but not `run_status` itself — but
            # it never reaches `run_events`, which is the one branch of
            # `resolve_ending` that would touch the warehouse.
            resolved = await resolve_ending(store, None, jobs, record.run_id, record.job_run_id)
        except Exception as exc:  # noqa: BLE001 - one run's failure must not end the tick
            report.errors.append(f"{record.run_id}: {exc}")
            log.warning("orphan sweep: could not resolve %s", record.run_id, exc_info=True)
            continue

        if resolved is None:
            if not record.job_run_id:
                # The one path in this module that can write a terminal
                # status without asking Databricks anything at all — see
                # `_never_launched` and the module docstring's closing
                # section for why that is safe here, and only here.
                try:
                    never_launched = await _never_launched(
                        store, record, now=now, never_launched_age_ms=never_launched_age_ms
                    )
                except Exception as exc:  # noqa: BLE001 - one run's failure must not end the tick
                    report.errors.append(f"{record.run_id}: {exc}")
                    log.warning(
                        "orphan sweep: could not check whether %s was ever launched",
                        record.run_id,
                        exc_info=True,
                    )
                    continue

                if never_launched is not None:
                    status, detail = never_launched
                    try:
                        await store.set_status(record.run_id, status, detail=detail)
                    except Exception as exc:  # noqa: BLE001 - try again next tick, not a crash
                        report.errors.append(
                            f"{record.run_id}: could not write corrected status: {exc}"
                        )
                        log.warning("orphan sweep: %s", report.errors[-1], exc_info=True)
                        continue
                    report.corrected.append((record.run_id, status.value))
                    log.info(
                        "orphan sweep released %s -> %s: never launched, no job_run_id and "
                        "no run_status_history after the age bound cleared (%s)",
                        record.run_id,
                        status.value,
                        detail,
                    )
            # Still running, no evidence either way (yet), or the Jobs API
            # could not answer — every one of those means "leave it alone",
            # never "assume the worst". It will be looked at again next tick.
            continue

        status, source_detail = resolved
        # Distinct from anything the job itself would write: a human reading
        # this run's history later should be able to tell "the job said this"
        # apart from "nobody heard from the job again, and Databricks says
        # the task ended" — the second is a materially weaker claim (no
        # result write is guaranteed to have happened) and should read as one.
        detail = (
            f"orphan sweep: {source_detail}; the job never reported its own ending "
            "(no live socket, nothing in run_status_history) — inferred, not reported"
        )
        try:
            await store.set_status(record.run_id, status, detail=detail)
        except Exception as exc:  # noqa: BLE001 - try again next tick rather than crash the loop
            report.errors.append(f"{record.run_id}: could not write corrected status: {exc}")
            log.warning("orphan sweep: %s", report.errors[-1], exc_info=True)
            continue

        report.corrected.append((record.run_id, status.value))
        log.warning(
            "orphan sweep corrected %s -> %s (%s); it was holding a task slot with "
            "no live socket and nothing reported",
            record.run_id,
            status.value,
            source_detail,
        )

    return report


class OrphanSweeper:
    """Owns the periodic tick: started once from `ServiceHub.startup`,
    stopped once from `ServiceHub.shutdown`. All of the judgment — what
    counts as suspicious, what to do about it, when to give up on a store or
    a dependency — lives in `sweep_once`; this only owns the schedule.

    One loop, one task, ticks strictly sequential (sleep, then tick, then
    sleep again) — never `create_task` per tick — so "one tick must not
    overlap the next" holds by construction and not by a lock this would
    otherwise need.
    """

    def __init__(
        self,
        *,
        store: RunStore | None,
        job_sockets: SocketRegistry,
        jobs_api: JobsApi | None,
        interval_s: float,
        min_age_s: float,
        socket_grace_s: float,
        never_launched_age_s: float | None = None,
    ) -> None:
        self._store = store
        self._job_sockets = job_sockets
        self._jobs_api = jobs_api
        self._interval_s = interval_s
        self._min_age_s = min_age_s
        self._socket_grace_s = socket_grace_s
        #: Deliberately not its own `AppConfig`/`DBX_ORPHAN_SWEEP_*` setting.
        #: `services.py` (which constructs this) is out of scope for this
        #: change, and a config field nothing reads would be exactly the
        #: "the deploy silently never sets it" failure `CLAUDE.md` already
        #: tells this story about once, for `DBX_LAKEBASE_*`. Deriving it
        #: from `min_age_s` instead keeps "much longer than min_age_s" true
        #: by construction for any deployment, with no second knob an
        #: operator could raise one of and forget the other. Optional and
        #: keyword-only so an explicit value — this file's own tests use
        #: one — always wins over the derivation.
        self._never_launched_age_s = (
            never_launched_age_s
            if never_launched_age_s is not None
            else max(
                min_age_s * _NEVER_LAUNCHED_AGE_MULTIPLIER,
                _NEVER_LAUNCHED_AGE_FLOOR_S,
            )
        )
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Idempotent: a second call while already running is a no-op rather
        than a second competing loop."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="orphan-sweep")

    async def stop(self) -> None:
        """Cancel and wait for the loop to actually exit before returning.

        `task.cancel()` alone would leave `ServiceHub.shutdown` racing the
        loop's own unwind — including, on the tick it happened to interrupt,
        a `store.set_status` write. `asyncio.sleep`, which is what the loop
        is blocked on between ticks the overwhelming majority of the time, is
        cancelled the instant `cancel()` is called — so this does not wait
        out `interval_s`; it only waits for whatever the current tick was
        doing (if anything) to unwind. That is what keeps `lifespan` from
        stalling shutdown on this, per its own requirement not to.
        """
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _loop(self) -> None:
        while True:
            # Sleep first, not tick-then-sleep: `reconcile_once` already ran
            # a full three-source pass moments ago, in the same startup, so a
            # tick right away would almost always find nothing — and every
            # test that spins up an app via `TestClient` now starts and stops
            # one of these, so a loop that ticks before it ever sleeps is a
            # loop that does real (if cheap) work on every such test rather
            # than only on ones that actually wait out an interval.
            await asyncio.sleep(self._interval_s)
            try:
                report = await sweep_once(
                    self._store,
                    self._job_sockets,
                    self._jobs_api,
                    min_age_s=self._min_age_s,
                    socket_grace_s=self._socket_grace_s,
                    never_launched_age_s=self._never_launched_age_s,
                )
                if report.corrected or report.errors:
                    log.info("orphan sweep: %r", report)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a tick must never end the loop
                # `sweep_once` already catches everything it knows the name
                # of; reaching here means something it did not anticipate.
                # Surviving that anyway is the entire point of a background
                # sweep — the alternative is a task slot stuck for good, with
                # nothing left running to notice or say so.
                log.exception("orphan sweep: tick failed; continuing")
