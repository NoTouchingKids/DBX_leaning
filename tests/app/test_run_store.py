"""The run store, driven against a real Postgres.

Lakebase is standard Postgres, so everything here except how the password is
obtained behaves identically to the real thing. Tested against PostgreSQL 16,
which is what this environment provides AND what the Lakebase instance created
on 2026-08-25 came back as — the CLI ignores `pg_version` on create, so 18 is
reachable only through the workspace UI. Nothing used here — primary keys,
ON CONFLICT, advisory locks, partial indexes — differs between 16 and 18, but
that is a claim about the feature set, not a test result. The app asserts
neither: `ensure_schema()` reads `SHOW server_version` and `/healthz` reports
what the server actually said.
"""

from __future__ import annotations

import asyncio
import pathlib
import tempfile

import pytest

from server.store import (
    DEFAULT_SCHEMA,
    DuplicateRun,
    PostgresRunStore,
    RunRecord,
    SlotDenied,
    UnsafeSchemaName,
    qualified,
)
from shared.envelope import RunStatus


def store_table() -> str:
    return qualified(DEFAULT_SCHEMA)


class RecordingSql:
    """Captures the SQL text and bound parameters, and answers nothing.

    The warehouse store's writes are what these tests are about, so the read
    path never runs — a MERGE is fire-and-forget here.
    """

    def __init__(self) -> None:
        self.queries: list[tuple[str, list]] = []

    async def query(self, sql, params=None):
        self.queries.append((sql, params or []))
        return []

    async def close(self): ...


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
        # Qualified, like every statement the store itself issues. An
        # unqualified name here resolves through `search_path` to a `public`
        # table that does not exist — which is the exact failure the schema
        # move was made to prevent, so the test may not depend on it either.
        await conn.execute(f"TRUNCATE {store_table()}")
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
        *(store.claim_slot(f"race-{i}", model="scenario", ceiling=3) for i in range(10)),
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


async def test_listing_can_be_filtered_by_model(store):
    await store.claim_slot("m1", model="mcmc", ceiling=5)
    await store.claim_slot("s1", model="scenario", ceiling=5)
    await store.claim_slot("m2", model="mcmc", ceiling=5)

    assert {r.run_id for r in await store.list_runs(model="mcmc")} == {"m1", "m2"}
    assert [r.run_id for r in await store.list_runs(model="scenario")] == ["s1"]
    assert await store.list_runs(model="nothing-runs-this") == []


async def test_status_and_model_filters_combine_rather_than_override(store):
    """Two optional filters is where branch-per-filter starts producing the
    wrong SQL: the second filter quietly replaces the first."""
    await store.claim_slot("m1", model="mcmc", ceiling=5)
    await store.claim_slot("m2", model="mcmc", ceiling=5)
    await store.claim_slot("s1", model="scenario", ceiling=5)
    await store.set_status("m2", RunStatus.SUCCEEDED)
    await store.set_status("s1", RunStatus.SUCCEEDED)

    both = await store.list_runs(status="SUCCEEDED", model="mcmc")
    assert [r.run_id for r in both] == ["m2"]


async def test_a_model_filter_is_a_bound_value_not_sql(store):
    await store.claim_slot("m1", model="mcmc", ceiling=5)

    hostile = "'; DROP TABLE run_status; --"
    assert await store.list_runs(model=hostile) == []
    # The table is still there, which it would not be under interpolation.
    assert [r.run_id for r in await store.list_runs()] == ["m1"]


async def test_non_terminal_is_what_reconciliation_reads(store):
    await store.claim_slot("done", model="scenario", ceiling=5)
    await store.claim_slot("live", model="scenario", ceiling=5)
    await store.set_status("done", RunStatus.SUCCEEDED)

    assert [r.run_id for r in await store.non_terminal()] == ["live"]


async def test_an_unknown_run_is_none_not_an_error(store):
    assert await store.get("never-existed") is None


async def test_the_store_reports_the_postgres_version_it_actually_got(tmp_path):
    """This repo once asserted "Lakebase runs PostgreSQL 18"; a real instance
    came back `PG_VERSION_16`, which is the default.

    The version is chosen at creation and immutable after, so a deployment can
    legitimately be on either and the only way to know is to ask. This asserts
    the answer is populated and looks like a version, not that it equals any
    particular one — pinning a number here would fail the day an instance is
    recreated on a different one, which is not a defect.
    """
    pgserver = pytest.importorskip("pgserver", reason="needs the dev group")
    server = pgserver.get_server(tmp_path / "pg")
    try:
        store = PostgresRunStore(server.get_uri())
        await store.ensure_schema()
        assert store.server_version is not None
        assert store.server_version[0].isdigit(), store.server_version
    finally:
        server.cleanup()


async def test_a_version_read_that_fails_does_not_break_startup():
    """A store that works but cannot report its version is strictly better
    than a startup that fails over a diagnostic."""

    class Boom:
        async def execute(self, *_a, **_k):
            raise RuntimeError("no SHOW for you")

    assert await PostgresRunStore._read_server_version(Boom()) is None


async def test_the_password_is_resolved_on_every_connection_not_once():
    """Lakebase's password is a short-lived OAuth token, so a DSN that carries
    one is valid for about an hour against an app that runs for up to 24.

    This is the assertion that the token is fetched per connection: two
    operations, two resolutions, and the second one gets the newer token.
    """
    tokens = iter(["tok-1", "tok-2", "tok-3"])
    seen: list[str] = []

    async def provider() -> str:
        value = next(tokens)
        seen.append(value)
        return value

    class FakeConn:
        async def execute(self, *_a, **_k):
            class Cur:
                async def fetchone(self):
                    return ("16.10",)

            return Cur()

        async def close(self):
            pass

    async def connect():
        # Mirrors what _conn does for real: resolve, then hand the value over.
        await provider()
        return FakeConn()

    store = PostgresRunStore("postgresql://pg/db", password_provider=provider, connect=connect)
    await store.ensure_schema()
    await store.ensure_schema()
    assert seen == ["tok-1", "tok-2"], "the credential was reused across connections"


async def test_no_provider_means_the_dsn_is_used_unchanged():
    """The local dev stack has no auth at all, and an instance with
    `enable_pg_native_login` on has a real password in the DSN."""
    store = PostgresRunStore("postgresql://pg/db")
    assert store._password_provider is None


# --- the schema the table lives in ----------------------------------------


async def test_the_table_is_not_in_public(postgres):
    """`public` is the failure this schema exists to avoid.

    Since PostgreSQL 15 the `public` schema no longer grants CREATE to
    `PUBLIC`, so a role that does not own the database — which the app's
    service principal generally does not — gets `permission denied for schema
    public` the first time `ensure_schema()` runs. The app reports `lakebase`
    degraded and falls back to the warehouse store, for a reason nobody
    debugging it would guess.
    """
    s = PostgresRunStore(postgres)
    await s.ensure_schema()

    conn = await s._conn()
    try:
        cur = await conn.execute("SELECT schemaname FROM pg_tables WHERE tablename = 'run_status'")
        schemas = [row[0] for row in await cur.fetchall()]
    finally:
        await conn.close()

    assert schemas == [DEFAULT_SCHEMA], f"run_status landed in {schemas}, not {DEFAULT_SCHEMA}"


async def test_a_custom_schema_is_honoured(postgres):
    s = PostgresRunStore(postgres, schema="other_place")
    await s.ensure_schema()
    await s.claim_slot("r1", model="scenario", ceiling=5)
    assert await s.active_count() == 1

    # And it really is a separate table, not the default one under a new name.
    default = PostgresRunStore(postgres)
    await default.ensure_schema()
    assert await default.active_count() == 0


@pytest.mark.parametrize(
    "bad",
    ["public; DROP TABLE run_status", "has-a-hyphen", "1_starts_with_digit", "", "a b"],
)
def test_a_schema_name_that_is_not_an_identifier_is_refused(bad):
    """The schema reaches SQL by interpolation because an identifier cannot be
    a bound parameter — the same reason `repository.validate_table_name`
    exists on the Unity Catalog side. Refused at construction, so a bad value
    fails while the app is starting and can report it."""
    with pytest.raises(UnsafeSchemaName):
        PostgresRunStore("postgresql://pg/db", schema=bad)
