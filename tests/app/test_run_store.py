"""The run store, driven against a real Postgres.

Lakebase is standard Postgres, so everything here except how the password is
obtained behaves identically to the real thing. Tested against PostgreSQL 16
(what this environment provides); Lakebase runs 18. Nothing used here —
primary keys, ON CONFLICT, advisory locks, partial indexes — changed between
those versions, but that is a claim about the feature set, not a test result.
"""

from __future__ import annotations

import asyncio
import pathlib
import tempfile

import pytest

from app.store import DuplicateRun, PostgresRunStore, RunRecord, SlotDenied
from shared.envelope import RunStatus

pgserver = pytest.importorskip("pgserver", reason="needs the dev group")


@pytest.fixture(scope="module")
def postgres():
    directory = pathlib.Path(tempfile.mkdtemp()) / "pg"
    server = pgserver.get_server(directory)
    try:
        yield server.get_uri()
    finally:
        server.cleanup()


@pytest.fixture
async def store(postgres):
    s = PostgresRunStore(postgres)
    await s.ensure_schema()
    conn = await s._conn()
    try:
        await conn.execute("TRUNCATE run_status")
    finally:
        await conn.close()
    return s


async def test_schema_creation_is_idempotent(store):
    await store.ensure_schema()
    await store.ensure_schema()
    assert await store.active_count() == 0


async def test_a_claimed_run_is_registered_as_queued(store):
    record = await store.claim_slot("r1", model="scenario", ceiling=5, requested_by="kp")

    assert record == RunRecord(
        run_id="r1",
        model="scenario",
        status=RunStatus.QUEUED,
        started_ts=record.started_ts,
        updated_ts=record.updated_ts,
        requested_by="kp",
    )
    stored = await store.get("r1")
    assert stored.status is RunStatus.QUEUED and stored.requested_by == "kp"
    assert await store.active_count() == 1


async def test_a_duplicate_run_id_is_refused(store):
    """Delta has no primary key, so this silently produced two rows for one
    run and the reader picked whichever came back first."""
    await store.claim_slot("r1", model="scenario", ceiling=5)
    with pytest.raises(DuplicateRun, match="already registered"):
        await store.claim_slot("r1", model="mcmc", ceiling=5)

    assert (await store.get("r1")).model == "scenario", "the first claim must stand"
    assert await store.active_count() == 1


async def test_the_ceiling_is_refused_with_the_numbers_in_it(store):
    for i in range(3):
        await store.claim_slot(f"r{i}", model="scenario", ceiling=3)

    with pytest.raises(SlotDenied) as exc:
        await store.claim_slot("r-over", model="scenario", ceiling=3)

    assert exc.value.active == 3 and exc.value.ceiling == 3
    assert "ceiling is 3" in str(exc.value)
    assert await store.get("r-over") is None, "a denied claim must leave nothing behind"


async def test_simultaneous_claims_cannot_both_pass_the_ceiling(store):
    """The race the warehouse implementation cannot win: count, then insert,
    with no transaction around the pair. Ten at once against a ceiling of
    three must yield exactly three."""
    await asyncio.gather(
        *(
            store.claim_slot(f"race-{i}", model="scenario", ceiling=3)
            for i in range(10)
        ),
        return_exceptions=True,
    )
    assert await store.active_count() == 3


async def test_finished_runs_free_their_slot(store):
    for i in range(3):
        await store.claim_slot(f"r{i}", model="scenario", ceiling=3)
    await store.set_status("r0", RunStatus.SUCCEEDED, detail="done")

    assert await store.active_count() == 2
    await store.claim_slot("r-new", model="scenario", ceiling=3)
    assert await store.active_count() == 3


@pytest.mark.parametrize(
    "status", [RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.INFEASIBLE]
)
async def test_every_terminal_status_frees_a_slot(store, status):
    await store.claim_slot("r1", model="scenario", ceiling=1)
    await store.set_status("r1", status)
    assert await store.active_count() == 0


async def test_status_transitions_are_recorded(store):
    await store.claim_slot("r1", model="mcmc", ceiling=5)
    await store.set_status("r1", RunStatus.RUNNING)
    await store.set_status("r1", "SUCCEEDED", detail="all draws done")

    record = await store.get("r1")
    assert record.status is RunStatus.SUCCEEDED and record.detail == "all draws done"
    assert record.terminal
    assert record.model == "mcmc", "a status write must not clobber the model"


async def test_a_status_for_an_unknown_run_creates_it(store):
    """A job can start while the app is down; its first status message may be
    the app's first sight of the run."""
    await store.set_status("appeared-from-nowhere", RunStatus.RUNNING)
    assert (await store.get("appeared-from-nowhere")).status is RunStatus.RUNNING


async def test_attaching_the_databricks_run_id(store):
    await store.claim_slot("r1", model="scenario", ceiling=5)
    await store.attach_job_run("r1", 987654)
    assert (await store.get("r1")).job_run_id == "987654"


async def test_releasing_a_slot_undoes_a_launch_that_failed(store):
    await store.claim_slot("r1", model="scenario", ceiling=5)
    await store.release_slot("r1")
    assert await store.get("r1") is None and await store.active_count() == 0


async def test_releasing_never_deletes_a_run_that_already_started(store):
    """Otherwise a late status write would resurrect a ghost row."""
    await store.claim_slot("r1", model="scenario", ceiling=5)
    await store.set_status("r1", RunStatus.RUNNING)
    await store.release_slot("r1")
    assert (await store.get("r1")) is not None


async def test_listing_is_newest_first_and_filterable(store):
    for i in range(3):
        await store.claim_slot(f"r{i}", model="scenario", ceiling=5)
    await store.set_status("r0", RunStatus.SUCCEEDED)

    everything = await store.list_runs(limit=10)
    assert {r.run_id for r in everything} == {"r0", "r1", "r2"}
    assert [r.updated_ts for r in everything] == sorted(
        (r.updated_ts for r in everything), reverse=True
    )

    succeeded = await store.list_runs(status="SUCCEEDED")
    assert [r.run_id for r in succeeded] == ["r0"]
    assert len(await store.list_runs(limit=2)) == 2


async def test_non_terminal_is_what_reconciliation_reads(store):
    await store.claim_slot("done", model="scenario", ceiling=5)
    await store.claim_slot("live", model="scenario", ceiling=5)
    await store.set_status("done", RunStatus.SUCCEEDED)

    assert [r.run_id for r in await store.non_terminal()] == ["live"]


async def test_an_unknown_run_is_none_not_an_error(store):
    assert await store.get("never-existed") is None
