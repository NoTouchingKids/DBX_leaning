"""The periodic orphan sweep: a run whose job died without telling anyone —
and, separately, a run whose *app* died before the job was ever launched.

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

#: The never-launched age bound used by every `sweep_once` call below that
#: does not care about it either way. Comfortably larger than `GRACE_S` —
#: the same relationship `OrphanSweeper` enforces by construction in
#: production (see its own docstring) — kept as an explicit constant here,
#: rather than derived, so each test states the threshold it is actually
#: exercising instead of relying on a hidden multiplier.
NEVER_LAUNCHED_GRACE_S = 1800.0


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

    def __init__(self, rows: list[RunRecord], *, transitions: list | None = None) -> None:
        self.rows = rows
        self.set: list[tuple[str, RunStatus, str | None]] = []
        self.non_terminal_calls = 0
        #: `run_status_history`, stood in for. Empty by default, matching a
        #: row nobody has ever reported a transition for — the ordinary case
        #: for every existing test in this file that does not pass one.
        self.transitions = list(transitions) if transitions else []
        self.history_calls = 0

    async def non_terminal(self, limit: int = 200) -> list[RunRecord]:
        self.non_terminal_calls += 1
        return list(self.rows)

    async def set_status(self, run_id, status, *, detail=None, ts=None) -> None:
        self.set.append((run_id, status, detail))

    async def history(self, run_id: str, *, limit: int = 500) -> list:
        self.history_calls += 1
        return [t for t in self.transitions if getattr(t, "run_id", None) == run_id]


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
        never_launched_age_s=NEVER_LAUNCHED_GRACE_S,
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
        store,
        FakeSockets(),
        terminated_jobs_api(),
        min_age_s=GRACE_S,
        socket_grace_s=GRACE_S,
        never_launched_age_s=NEVER_LAUNCHED_GRACE_S,
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
        store,
        FakeSockets(),
        terminated_jobs_api(),
        min_age_s=GRACE_S,
        socket_grace_s=GRACE_S,
        never_launched_age_s=NEVER_LAUNCHED_GRACE_S,
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
        never_launched_age_s=NEVER_LAUNCHED_GRACE_S,
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
        never_launched_age_s=NEVER_LAUNCHED_GRACE_S,
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
        never_launched_age_s=NEVER_LAUNCHED_GRACE_S,
    )
    assert report.corrected == [("r1", "CANCELLED")]


async def test_an_old_socketless_run_still_running_is_left_alone():
    store = FakeStore([old_record("r1")])
    report = await sweep_once(
        store,
        FakeSockets(),
        running_jobs_api(),
        min_age_s=GRACE_S,
        socket_grace_s=GRACE_S,
        never_launched_age_s=NEVER_LAUNCHED_GRACE_S,
    )
    assert report.checked == 1
    assert report.corrected == []
    assert store.set == []


# --------------------------------------------------------------------------
# sweep_once: a claim with no job_run_id at all — never launched, or just
# not heard from yet?
#
# `routes/runs.py::trigger_run` writes this row (`claim_slot`) strictly
# before it calls `run_now`. If the app dies between the two — or in the
# narrow window right after, before `attach_job_run` lands — the row is left
# non-terminal with `job_run_id = NULL` forever: `resolve_ending` has no
# Databricks run to ask the Jobs API about, and nothing else in this app
# will ever supply one after the fact. `_never_launched`
# (`server/orphan_sweep.py`) is the only thing that can still close that row,
# and only when both an age bound and a `run_status_history` check agree
# there is nothing else to wait for. A `JobsApiThatMustNotBeAsked` is used
# throughout this section, deliberately: every one of these rows has no
# `job_run_id`, so `resolve_ending` already never asks the Jobs API about
# them (existing behaviour, unchanged) — using an API that fails loudly if
# that ever happens keeps this section honest about it.
# --------------------------------------------------------------------------


class JobsApiThatMustNotBeAsked(JobsApi):
    """A row with no `job_run_id` has no Databricks run to ask about at all
    — `resolve_ending` already knows this. Used across this section so a
    regression that starts asking anyway fails loudly instead of by
    coincidence passing because the canned answer happened to agree."""

    async def get_run(self, job_run_id):
        raise AssertionError("the Jobs API must not be asked about a run with no job_run_id")


async def test_a_run_with_no_job_run_id_and_no_history_is_released_as_never_launched():
    """This test used to be named `..._is_left_alone_not_guessed_at`, and
    pinned the opposite behaviour — this is the rule that changed, and this
    is the test that pins the replacement.

    `claim_slot` runs before `run_now`, deliberately, so a job can never be
    running with the registry unaware of it (see `trigger_run`'s own
    docstring). The cost of that ordering is this row: if the app dies right
    after `claim_slot`, nothing is ever going to call `run_now`, so no
    Databricks run will ever exist for the Jobs API to be asked about, and no
    restart or amount of waiting would otherwise produce an answer. Left
    alone, this is a task slot gone for good — one of Free Edition's
    account-wide ceiling of five — with no way back short of editing the
    table by hand.

    Comfortably old, no `job_run_id`, and — the fact this test is really
    pinning — nothing in `run_status_history` either, which is the cheap,
    already-available evidence that tells "never launched" apart from
    "launched, and has not said anything since". Breaking this (for
    instance, reverting `_never_launched` to always return `None`) silently
    reopens exactly that bug: a claim nobody can ever account for, burning
    20% of the platform's entire job concurrency until someone finds it by
    hand.
    """
    store = FakeStore([old_record("r1", job_run_id=None)])  # age_s=3600, no history
    report = await sweep_once(
        store,
        FakeSockets(),
        JobsApiThatMustNotBeAsked("https://x", "t"),
        min_age_s=GRACE_S,
        socket_grace_s=GRACE_S,
        never_launched_age_s=NEVER_LAUNCHED_GRACE_S,
    )
    assert report.corrected == [("r1", "CANCELLED")]
    run_id, status, detail = store.set[0]
    assert (run_id, status) == ("r1", RunStatus.CANCELLED)
    # The policy's own words, plainly stated in the record: never launched,
    # and inferred from the absence of a job run id — not a status any job
    # reported.
    assert "never launched" in detail
    assert "job run id" in detail
    assert "not reported" in detail


async def test_a_run_with_no_job_run_id_that_has_not_cleared_the_never_launched_age_is_left_alone():
    """Old enough for the ordinary orphan grace periods (`min_age_s` /
    `socket_grace_s`) is not old enough for this one: the never-launched
    bound is deliberately a much larger multiple of `min_age_s` (see
    `server/orphan_sweep.py`'s module docstring), specifically so a
    `trigger_run` that is merely slow — a retried Jobs API call, a slow
    Lakebase write — can never be mistaken for one that crashed outright.
    Distinct from `test_a_young_run_is_never_swept`, which pins the older,
    much shorter `min_age_s` rule; this pins the new, much longer one."""
    store = FakeStore([old_record("r1", job_run_id=None, age_s=GRACE_S * 2)])
    report = await sweep_once(
        store,
        FakeSockets(),
        JobsApiThatMustNotBeAsked("https://x", "t"),
        min_age_s=GRACE_S,
        socket_grace_s=GRACE_S,
        never_launched_age_s=NEVER_LAUNCHED_GRACE_S,
    )
    assert report.checked == 1
    assert report.corrected == []
    assert store.set == []


async def test_a_run_with_no_job_run_id_but_some_history_is_never_treated_as_unlaunched():
    """Any row in `run_status_history` — even a bare, non-terminal one — is
    proof the job got as far as reporting *something*, which means it did
    launch, whatever happens to it next. `_never_launched` must not release a
    run just because `resolve_ending` could not find a *terminal* transition
    to resolve it with; those are different questions (see
    `_reported_anything`'s own docstring), and only "nothing at all" may
    release a slot with no other source left to check."""

    class Reported:
        def __init__(self, run_id: str) -> None:
            self.run_id = run_id

    store = FakeStore([old_record("r1", job_run_id=None)], transitions=[Reported("r1")])
    report = await sweep_once(
        store,
        FakeSockets(),
        JobsApiThatMustNotBeAsked("https://x", "t"),
        min_age_s=GRACE_S,
        socket_grace_s=GRACE_S,
        never_launched_age_s=NEVER_LAUNCHED_GRACE_S,
    )
    assert report.corrected == []
    assert store.set == []


async def test_a_run_with_no_job_run_id_is_left_alone_when_history_cannot_be_read():
    """An unreadable answer is not the same as a confirmed-empty one — the
    same rule `resolve_ending` applies to a Jobs API that could not answer
    applies here too: "leave it alone, try again next tick", never "assume
    the worst". This is the one path in the sweep that could otherwise end a
    run that is genuinely still in flight, so a transient Postgres error must
    not be read as permission to do that."""

    class UnreadableHistory(FakeStore):
        async def history(self, run_id, *, limit=500):
            raise RuntimeError("lakebase unreachable")

    store = UnreadableHistory([old_record("r1", job_run_id=None)])
    report = await sweep_once(
        store,
        FakeSockets(),
        JobsApiThatMustNotBeAsked("https://x", "t"),
        min_age_s=GRACE_S,
        socket_grace_s=GRACE_S,
        never_launched_age_s=NEVER_LAUNCHED_GRACE_S,
    )
    assert report.corrected == []
    assert store.set == []


async def test_a_run_with_no_job_run_id_is_left_alone_when_the_store_has_no_history_method_at_all():
    """`history` is deliberately not on the `RunStore` Protocol (see
    `reconcile.py`'s note on the same point) — a store that lacks it entirely
    must read as "cannot tell", the same as one whose `history()` raises, not
    as "confirmed nothing happened"."""

    class NoHistoryAtAll:
        name = "postgres"

        def __init__(self, rows: list[RunRecord]) -> None:
            self.rows = rows
            self.set: list[tuple[str, RunStatus, str | None]] = []

        async def non_terminal(self, limit: int = 200) -> list[RunRecord]:
            return list(self.rows)

        async def set_status(self, run_id, status, *, detail=None, ts=None) -> None:
            self.set.append((run_id, status, detail))

    store = NoHistoryAtAll([old_record("r1", job_run_id=None)])
    report = await sweep_once(
        store,
        FakeSockets(),
        JobsApiThatMustNotBeAsked("https://x", "t"),
        min_age_s=GRACE_S,
        socket_grace_s=GRACE_S,
        never_launched_age_s=NEVER_LAUNCHED_GRACE_S,
    )
    assert report.corrected == []
    assert store.set == []


async def test_a_run_with_a_job_run_id_resolves_via_the_existing_path_not_the_never_launched_one():
    """A row with a real `job_run_id` has a Databricks run for
    `resolve_ending`'s Jobs API step to ask about, and must resolve there,
    untouched by anything this task added. `never_launched_age_s=0` is
    deliberately provocative: if the `job_run_id` gate in `sweep_once` were
    ever accidentally loosened, a zero threshold would make the never-launched
    path fire immediately, so this fails fast instead of by coincidence
    passing. `history_calls` stays at exactly the one call `_from_history`
    (inside `resolve_ending`) always makes regardless of `job_run_id` — a
    second call would mean `_never_launched`'s own history check ran when it
    must not have."""
    store = FakeStore([old_record("r1", job_run_id="99")])
    report = await sweep_once(
        store,
        FakeSockets(),
        terminated_jobs_api("SUCCESS"),
        min_age_s=GRACE_S,
        socket_grace_s=GRACE_S,
        never_launched_age_s=0,
    )
    assert report.corrected == [("r1", "SUCCEEDED")]
    assert store.history_calls == 1
    run_id, status, detail = store.set[0]
    assert (run_id, status) == ("r1", RunStatus.SUCCEEDED)
    # The existing Jobs-API-resolution wording, not the new never-launched one.
    assert "jobs api" in detail
    assert "never launched" not in detail


# --------------------------------------------------------------------------
# sweep_once: degraded dependencies never crash a tick
# --------------------------------------------------------------------------


async def test_no_store_configured_is_a_safe_no_op():
    report = await sweep_once(
        None,
        FakeSockets(),
        running_jobs_api(),
        min_age_s=0,
        socket_grace_s=0,
        never_launched_age_s=0,
    )
    assert report.checked == 0
    assert report.errors == []


async def test_no_jobs_api_configured_is_a_safe_no_op():
    """Nothing to confirm a death against, so nothing happens — and the store
    is not even read, since there is no point listing runs with no way to
    check any of them."""
    store = FakeStore([old_record("r1")])
    report = await sweep_once(
        store, FakeSockets(), None, min_age_s=0, socket_grace_s=0, never_launched_age_s=0
    )
    assert report.checked == 0
    assert store.set == []
    assert store.non_terminal_calls == 0


async def test_a_store_that_fails_to_list_runs_is_reported_not_raised():
    class BrokenStore:
        name = "postgres"

        async def non_terminal(self):
            raise RuntimeError("lakebase unreachable")

    report = await sweep_once(
        BrokenStore(),
        FakeSockets(),
        running_jobs_api(),
        min_age_s=0,
        socket_grace_s=0,
        never_launched_age_s=0,
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
        store,
        FakeSockets(),
        RaisingJobsApi(),
        min_age_s=GRACE_S,
        socket_grace_s=GRACE_S,
        never_launched_age_s=NEVER_LAUNCHED_GRACE_S,
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
        store,
        FakeSockets(),
        terminated_jobs_api(),
        min_age_s=GRACE_S,
        socket_grace_s=GRACE_S,
        never_launched_age_s=NEVER_LAUNCHED_GRACE_S,
    )
    assert report.corrected == []
    assert report.errors


async def test_a_failed_status_write_is_recorded_not_raised_on_the_never_launched_path_too():
    """The same "log it, try again next tick" contract the ordinary
    resolution path gets must hold for `_never_launched`'s own write — a
    write failure here must not raise into `OrphanSweeper._loop`."""

    class RefusingStore(FakeStore):
        async def set_status(self, run_id, status, *, detail=None, ts=None):
            raise RuntimeError("write refused")

    store = RefusingStore([old_record("r1", job_run_id=None)])
    report = await sweep_once(
        store,
        FakeSockets(),
        JobsApiThatMustNotBeAsked("https://x", "t"),
        min_age_s=GRACE_S,
        socket_grace_s=GRACE_S,
        never_launched_age_s=NEVER_LAUNCHED_GRACE_S,
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
        store,
        FakeSockets(),
        running_jobs_api(),
        min_age_s=0,
        socket_grace_s=0,
        never_launched_age_s=0,
    )

    assert repo.calls == 0, "the warehouse must never be read on a timer"
    assert report.checked == 0


# --------------------------------------------------------------------------
# OrphanSweeper: deriving never_launched_age_s
#
# `services.py` constructs `OrphanSweeper` with `interval_s`/`min_age_s`/
# `socket_grace_s` only — no `AppConfig` setting threads a `never_launched_
# age_s` through it, deliberately (see `OrphanSweeper.__init__`'s own
# comment): a config field `services.py` never reads would be exactly the
# "the deploy silently never sets it" failure `CLAUDE.md` already tells this
# story about once, for `DBX_LAKEBASE_*`. These tests pin the derivation that
# stands in for that setting instead.
# --------------------------------------------------------------------------


def test_orphansweeper_derives_a_never_launched_age_far_larger_than_min_age_when_none_is_given():
    sweeper = OrphanSweeper(
        store=None,
        job_sockets=FakeSockets(),
        jobs_api=None,
        interval_s=60,
        min_age_s=180,
        socket_grace_s=150,
    )
    assert sweeper._never_launched_age_s >= 10 * 180


def test_orphansweeper_never_launched_age_has_a_floor_even_when_min_age_is_tuned_to_zero():
    """`min_age_s=0` is common in this very file's own tests, and a real
    deployment might reasonably tune it low too. A pure multiplier would
    derive a never-launched bound of zero right along with it — the floor is
    what stops that."""
    sweeper = OrphanSweeper(
        store=None,
        job_sockets=FakeSockets(),
        jobs_api=None,
        interval_s=60,
        min_age_s=0,
        socket_grace_s=0,
    )
    assert sweeper._never_launched_age_s >= 1800.0


def test_an_explicit_never_launched_age_passed_to_orphansweeper_overrides_the_derivation():
    sweeper = OrphanSweeper(
        store=None,
        job_sockets=FakeSockets(),
        jobs_api=None,
        interval_s=60,
        min_age_s=180,
        socket_grace_s=150,
        never_launched_age_s=42,
    )
    assert sweeper._never_launched_age_s == 42


async def test_servicehub_still_constructs_the_orphansweeper_with_no_config_change_needed():
    """`services.py::ServiceHub._start_orphan_sweep` is the one call site this
    task could not edit — no other test in this repo exercises it (it needs
    real startup, which the `app_and_hub` fixture deliberately skips to stay
    free of I/O). Called here directly, unmodified: `ServiceHub.__init__` sets
    `store`/`jobs_api` to `None` and `job_sockets` to a real, local
    `JobConnections()` with no I/O of its own, so this is cheap and needs no
    mocking. What this actually proves is that `never_launched_age_s` being
    optional on `OrphanSweeper.__init__` is not just a claim: `services.py`'s
    six-keyword call, exactly as it is written today, still constructs a
    working sweeper and derives the same age bound `OrphanSweeper` would on
    its own."""
    from server.config import AppConfig
    from server.services import ServiceHub

    cfg = AppConfig(orphan_sweep_min_age_s=180.0)
    hub = ServiceHub(cfg)
    hub._start_orphan_sweep(cfg)
    try:
        assert hub._orphan_sweeper is not None
        assert hub._orphan_sweeper._never_launched_age_s == 1800.0
    finally:
        await hub._orphan_sweeper.stop()


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
