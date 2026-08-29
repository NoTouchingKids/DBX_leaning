"""The periodic orphan sweep: a run whose job died without telling anyone.

Fakes here are deliberately minimal and hand-rolled, matching
`test_reconcile_and_deps.py` rather than `conftest.py`'s `app_and_hub` — this
is about `sweep_once`/`OrphanSweeper`'s own logic, not HTTP wiring, so there
is no need to build a whole app for it.
"""

from __future__ import annotations

import asyncio

from server.jobs_api import JobsApi
from server.orphan_sweep import OrphanSweeper, sweep_once
from server.store import RunRecord, WarehouseRunStore
from shared.envelope import RunStatus, now_ms

from .conftest import FakeHttp

#: Generous relative to the ages used below (seconds vs. an hour), so a test
#: asserting "old enough" is never accidentally testing "grace is zero".
GRACE_S = 60.0


class FakeSockets:
    """`hub.job_sockets`, stood in for: `is_connected` and nothing else —
    the one fact `sweep_once` needs from it."""

    def __init__(self, live: frozenset[str] = frozenset()) -> None:
        self.live = set(live)

    def is_connected(self, run_id: str) -> bool:
        return run_id in self.live


class FakeStore:
    """A run store with no warehouse behind it — i.e. Lakebase. `.name` is
    what `sweep_once` gates on, so it is set explicitly rather than inherited
    from a real class, the same way `test_reconcile_and_deps.py`'s
    `MemoryStore` stands in for Postgres without being it."""

    name = "postgres"

    def __init__(self, rows: list[RunRecord]) -> None:
        self.rows = rows
        self.set: list[tuple[str, RunStatus, str | None]] = []
        self.non_terminal_calls = 0

    async def non_terminal(self, limit: int = 200) -> list[RunRecord]:
        self.non_terminal_calls += 1
        return list(self.rows)

    async def set_status(self, run_id, status, *, detail=None, ts=None) -> None:
        self.set.append((run_id, status, detail))


def young_record(run_id: str, *, job_run_id: str | None = "99") -> RunRecord:
    """Claimed right now — the ordinary shape of a run that has not attached
    yet, per `orphan_sweep_min_age_s`."""
    now = now_ms()
    return RunRecord(
        run_id=run_id,
        model="mcmc",
        status=RunStatus.RUNNING,
        job_run_id=job_run_id,
        started_ts=now,
        updated_ts=now,
    )


def old_record(run_id: str, *, job_run_id: str | None = "99", age_s: float = 3600) -> RunRecord:
    """Claimed and last updated `age_s` ago — comfortably clear of both grace
    periods used in these tests."""
    ts = now_ms() - int(age_s * 1000)
    return RunRecord(
        run_id=run_id,
        model="mcmc",
        status=RunStatus.RUNNING,
        job_run_id=job_run_id,
        started_ts=ts,
        updated_ts=ts,
    )


def terminated_jobs_api(code: str = "SUCCESS") -> JobsApi:
    http = FakeHttp({"status": {"state": "TERMINATED", "termination_details": {"code": code}}})
    return JobsApi("https://ws.example.com", "tok", client=http)


def running_jobs_api() -> JobsApi:
    http = FakeHttp({"status": {"state": "RUNNING"}})
    return JobsApi("https://ws.example.com", "tok", client=http)


# --------------------------------------------------------------------------
# sweep_once: the grace rules
# --------------------------------------------------------------------------


async def test_a_run_with_a_live_socket_is_never_swept():
    """Live by definition — whatever else is true, something is talking to
    it right now, so it must never even reach the Jobs API."""
    store = FakeStore([old_record("r1")])
    report = await sweep_once(
        store,
        FakeSockets(live=frozenset({"r1"})),
        terminated_jobs_api(),
        min_age_s=GRACE_S,
        socket_grace_s=GRACE_S,
    )
    assert report.skipped_live == 1
    assert report.checked == 0
    assert store.set == []


async def test_a_young_run_is_never_swept():
    """`run-now` returns before a serverless task has even started, so a run
    that was claimed moments ago is the ordinary shape of "just launched",
    not a corpse — it must not reach the Jobs API either."""
    store = FakeStore([young_record("r1")])
    report = await sweep_once(
        store, FakeSockets(), terminated_jobs_api(), min_age_s=GRACE_S, socket_grace_s=GRACE_S
    )
    assert report.skipped_too_recent == 1
    assert report.checked == 0
    assert store.set == []


async def test_an_old_run_updated_moments_ago_is_still_given_reconnect_room():
    """The socket-drop grace is a separate rule from the age rule: a run that
    has been going for an hour but transitioned RUNNING five seconds ago must
    still be given room for the job's own reconnect timer before a missing
    socket is trusted as anything more than that."""
    row = RunRecord(
        run_id="r1",
        model="mcmc",
        status=RunStatus.RUNNING,
        job_run_id="99",
        started_ts=now_ms() - 3_600_000,
        updated_ts=now_ms() - 5_000,
    )
    store = FakeStore([row])
    report = await sweep_once(
        store, FakeSockets(), terminated_jobs_api(), min_age_s=GRACE_S, socket_grace_s=GRACE_S
    )
    assert report.skipped_too_recent == 1
    assert store.set == []


# --------------------------------------------------------------------------
# sweep_once: resolving a candidate that clears both grace periods
# --------------------------------------------------------------------------


async def test_an_old_socketless_run_the_jobs_api_says_ended_gets_corrected():
    store = FakeStore([old_record("r1")])
    report = await sweep_once(
        store,
        FakeSockets(),
        terminated_jobs_api("SUCCESS"),
        min_age_s=GRACE_S,
        socket_grace_s=GRACE_S,
    )
    assert report.corrected == [("r1", "SUCCEEDED")]
    run_id, status, detail = store.set[0]
    assert (run_id, status) == ("r1", RunStatus.SUCCEEDED)
    # Distinguishable from a status the job reported itself — an operator (or
    # a client rendering `run.detail`) should be able to tell "the job said
    # this" apart from "nobody heard from the job again".
    assert "orphan sweep" in detail
    assert "jobs api" in detail


async def test_lakebase_history_resolves_a_run_without_ever_asking_the_jobs_api():
    """`sweep_once` reuses `resolve_ending` with `repo=None`, not a copy of
    its Jobs-API-only step — so the rare case where the job managed to append
    to `run_status_history` but not upsert `run_status` itself must resolve
    from that history for free, exactly as `reconcile_once` would, without
    ever reaching the Jobs API this test would otherwise be able to see was
    asked (`RunningJobsApiThatMustNotBeAsked.get_run` would return RUNNING and
    the test would then wrongly leave the run uncorrected if that path were
    somehow taken)."""

    class StoreWithHistory(FakeStore):
        def __init__(self, rows, transitions):
            super().__init__(rows)
            self.transitions = transitions

        async def history(self, run_id, *, limit=500):
            return [t for t in self.transitions if t.run_id == run_id]

    class StatusTransition:
        def __init__(self, run_id, status, ts):
            self.run_id = run_id
            self.status = status
            self.ts = ts
            self.id = 0

    class RunningJobsApiThatMustNotBeAsked(JobsApi):
        async def get_run(self, job_run_id):
            raise AssertionError("the Jobs API must not be asked when history already answers")

    store = StoreWithHistory(
        [old_record("r1")], [StatusTransition("r1", "FAILED", now_ms())]
    )
    report = await sweep_once(
        store,
        FakeSockets(),
        RunningJobsApiThatMustNotBeAsked("https://x", "t"),
        min_age_s=GRACE_S,
        socket_grace_s=GRACE_S,
    )
    assert report.corrected == [("r1", "FAILED")]
    assert "run_status_history" in store.set[0][2]


async def test_a_cancelled_databricks_run_maps_to_cancelled_not_failed():
    """The same escape-hatch mapping `reconcile_once` relies on
    (`databricks jobs cancel-run` reports `USER_CANCELED`) must hold here too,
    since both go through the same `resolve_ending`."""
    store = FakeStore([old_record("r1")])
    report = await sweep_once(
        store,
        FakeSockets(),
        terminated_jobs_api("USER_CANCELED"),
        min_age_s=GRACE_S,
        socket_grace_s=GRACE_S,
    )
    assert report.corrected == [("r1", "CANCELLED")]


async def test_an_old_socketless_run_still_running_is_left_alone():
    store = FakeStore([old_record("r1")])
    report = await sweep_once(
        store, FakeSockets(), running_jobs_api(), min_age_s=GRACE_S, socket_grace_s=GRACE_S
    )
    assert report.checked == 1
    assert report.corrected == []
    assert store.set == []


async def test_a_run_with_no_job_run_id_yet_is_left_alone_not_guessed_at():
    """A candidate that cleared both grace periods but was never attached to
    a Databricks run id (the app could have died between `claim_slot` and
    `attach_job_run`) has nothing for the Jobs API to answer about.
    `resolve_ending` already treats that as "no answer"; this pins that number
    the sweep does not invent a status to fill the gap."""
    store = FakeStore([old_record("r1", job_run_id=None)])
    report = await sweep_once(
        store, FakeSockets(), terminated_jobs_api(), min_age_s=GRACE_S, socket_grace_s=GRACE_S
    )
    assert report.checked == 1
    assert report.corrected == []
    assert store.set == []


# --------------------------------------------------------------------------
# sweep_once: degraded dependencies never crash a tick
# --------------------------------------------------------------------------


async def test_no_store_configured_is_a_safe_no_op():
    report = await sweep_once(
        None, FakeSockets(), running_jobs_api(), min_age_s=0, socket_grace_s=0
    )
    assert report.checked == 0
    assert report.errors == []


async def test_no_jobs_api_configured_is_a_safe_no_op():
    """Nothing to confirm a death against, so nothing happens — and the store
    is not even read, since there is no point listing runs with no way to
    check any of them."""
    store = FakeStore([old_record("r1")])
    report = await sweep_once(store, FakeSockets(), None, min_age_s=0, socket_grace_s=0)
    assert report.checked == 0
    assert store.set == []
    assert store.non_terminal_calls == 0


async def test_a_store_that_fails_to_list_runs_is_reported_not_raised():
    class BrokenStore:
        name = "postgres"

        async def non_terminal(self):
            raise RuntimeError("lakebase unreachable")

    report = await sweep_once(
        BrokenStore(), FakeSockets(), running_jobs_api(), min_age_s=0, socket_grace_s=0
    )
    assert report.checked == 0
    assert report.errors


async def test_a_raising_jobs_api_does_not_stop_other_runs_in_the_same_tick():
    """One run's failure must not end the tick for the runs after it."""

    class RaisingJobsApi:
        async def get_run(self, job_run_id):
            raise RuntimeError("workspace unreachable")

    store = FakeStore([old_record("r-a", job_run_id="1"), old_record("r-b", job_run_id="2")])
    report = await sweep_once(
        store, FakeSockets(), RaisingJobsApi(), min_age_s=GRACE_S, socket_grace_s=GRACE_S
    )
    assert report.checked == 2
    assert len(report.errors) == 2
    assert store.set == []


async def test_a_failed_status_write_is_recorded_not_raised():
    class RefusingStore(FakeStore):
        async def set_status(self, run_id, status, *, detail=None, ts=None):
            raise RuntimeError("write refused")

    store = RefusingStore([old_record("r1")])
    report = await sweep_once(
        store, FakeSockets(), terminated_jobs_api(), min_age_s=GRACE_S, socket_grace_s=GRACE_S
    )
    assert report.corrected == []
    assert report.errors


# --------------------------------------------------------------------------
# sweep_once: the warehouse-backed store is skipped outright
# --------------------------------------------------------------------------


async def test_the_warehouse_backed_store_is_skipped_without_touching_it():
    """`WarehouseRunStore.non_terminal` reads Delta over the SQL warehouse.
    Calling that on a timer would be the exact cost mistake this whole sweep
    exists to avoid, just relocated from a status poll to an orphan sweep —
    so this store is skipped outright rather than swept in a degraded way."""

    class CountingRepo:
        def __init__(self) -> None:
            self.calls = 0

        async def non_terminal_runs(self, limit):
            self.calls += 1
            return []

    repo = CountingRepo()
    store = WarehouseRunStore(repo)
    assert store.name == "warehouse"

    report = await sweep_once(
        store, FakeSockets(), running_jobs_api(), min_age_s=0, socket_grace_s=0
    )

    assert repo.calls == 0, "the warehouse must never be read on a timer"
    assert report.checked == 0


# --------------------------------------------------------------------------
# OrphanSweeper: the scheduling loop
# --------------------------------------------------------------------------


async def test_the_task_stops_cleanly_on_shutdown():
    store = FakeStore([])
    sweeper = OrphanSweeper(
        store=store,
        job_sockets=FakeSockets(),
        jobs_api=running_jobs_api(),
        interval_s=0.01,
        min_age_s=0,
        socket_grace_s=0,
    )
    sweeper.start()
    await asyncio.sleep(0.1)  # several ticks' worth of headroom
    ticks_before_stop = store.non_terminal_calls
    assert ticks_before_stop >= 2, "the loop should have ticked more than once by now"

    await sweeper.stop()
    assert sweeper._task is None

    await asyncio.sleep(0.05)
    assert store.non_terminal_calls == ticks_before_stop, "no tick ran after stop() returned"


async def test_stop_before_start_and_double_stop_are_both_safe_no_ops():
    sweeper = OrphanSweeper(
        store=None,
        job_sockets=FakeSockets(),
        jobs_api=None,
        interval_s=60,
        min_age_s=0,
        socket_grace_s=0,
    )
    await sweeper.stop()  # never started
    sweeper.start()
    await sweeper.stop()
    await sweeper.stop()  # already stopped


async def test_start_is_idempotent():
    """A second `start()` while already running must not spin up a second
    competing loop — that would break "one tick must not overlap the next"."""
    store = FakeStore([])
    sweeper = OrphanSweeper(
        store=store,
        job_sockets=FakeSockets(),
        jobs_api=running_jobs_api(),
        interval_s=0.01,
        min_age_s=0,
        socket_grace_s=0,
    )
    sweeper.start()
    first_task = sweeper._task
    sweeper.start()
    assert sweeper._task is first_task
    await sweeper.stop()


async def test_a_tick_that_raises_outside_sweep_ones_own_handling_does_not_kill_the_loop():
    """Defence in depth for `_loop`'s own `except Exception`, distinct from
    `sweep_once`'s internal handling: even something `sweep_once` did not
    anticipate must not end the background task."""

    class ExplodingSockets:
        def is_connected(self, run_id: str) -> bool:
            raise RuntimeError("boom")

    store = FakeStore([old_record("r1")])
    sweeper = OrphanSweeper(
        store=store,
        job_sockets=ExplodingSockets(),
        jobs_api=running_jobs_api(),
        interval_s=0.01,
        min_age_s=0,
        socket_grace_s=0,
    )
    sweeper.start()
    await asyncio.sleep(0.1)
    ticks_before_stop = store.non_terminal_calls
    assert ticks_before_stop >= 2, "a tick's exception must not end the loop"

    await sweeper.stop()
    assert sweeper._task is None


async def test_a_raising_jobs_api_does_not_kill_the_loop_across_ticks():
    class RaisingJobsApi:
        async def get_run(self, job_run_id):
            raise RuntimeError("workspace unreachable")

    store = FakeStore([old_record("r1")])
    sweeper = OrphanSweeper(
        store=store,
        job_sockets=FakeSockets(),
        jobs_api=RaisingJobsApi(),
        interval_s=0.01,
        min_age_s=0,
        socket_grace_s=0,
    )
    sweeper.start()
    await asyncio.sleep(0.1)
    ticks_before_stop = store.non_terminal_calls
    assert ticks_before_stop >= 2, "the Jobs API raising must not end the loop"
    assert store.set == []

    await sweeper.stop()
    assert sweeper._task is None
