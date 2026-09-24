"""The run store, driven against a real Postgres.

Lakebase is standard Postgres, so everything here except how the password is
obtained behaves identically to the real thing. Tested against PostgreSQL 16,
which is what this environment provides AND what the Lakebase instance created
on 2026-08-25 came back as — the CLI ignores `pg_version` on create, so 18 is
reachable only through the workspace UI. Nothing used here — primary keys,
ON CONFLICT, partial indexes — differs between 16 and 18, but
that is a claim about the feature set, not a test result. The app asserts
neither: `ensure_schema()` reads `SHOW server_version` and `/healthz` reports
what the server actually said.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from server.store import (
    DEFAULT_SCHEMA,
    PostgresRunStore,
    UnsafeSchemaName,
    qualified,
)
from shared.envelope import RunStatus


def store_table() -> str:
    return qualified(DEFAULT_SCHEMA)


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


async def seed(store: PostgresRunStore, run_id: str, *, model: str) -> None:
    """A row with a model name, written the way no production path writes one.

    `claim_slot` used to be what set `model`, and it had no callers, so it was
    removed in v5 — which leaves `set_status`'s upsert, inserting `''`, as the
    only writer. That is a real gap for Phase 2 of `docs/v5-implementation-plan.md`
    to close. The listing filters below are still correct SQL worth pinning,
    so they seed the column directly rather than waiting for that.
    """
    conn = await store._conn()
    try:
        await conn.execute(
            f"INSERT INTO {store_table()} (run_id, model, status, started_ts, updated_ts) "
            "VALUES (%s, %s, %s, 1, 1)",
            (run_id, model, RunStatus.QUEUED),
        )
    finally:
        await conn.close()


async def test_schema_creation_is_idempotent(store):
    await store.ensure_schema()
    await store.ensure_schema()
    assert await store.list_runs() == []


async def test_status_transitions_are_recorded(store):
    await seed(store, "r1", model="mcmc")
    await store.set_status("r1", RunStatus.RUNNING)
    await store.set_status("r1", "SUCCEEDED", detail="all draws done")

    record = await store.get("r1")
    assert record.status == RunStatus.SUCCEEDED and record.detail == "all draws done"
    assert record.terminal
    assert record.model == "mcmc", "a status write must not clobber the model"


async def test_a_status_for_an_unknown_run_creates_it(store):
    """A job can start while the app is down; its first status message may be
    the app's first sight of the run."""
    await store.set_status("appeared-from-nowhere", RunStatus.RUNNING)
    assert (await store.get("appeared-from-nowhere")).status == RunStatus.RUNNING


async def test_a_run_id_is_one_row_however_often_it_is_written(store):
    """The primary key is the one property Postgres was chosen for that is
    still in use: Delta had none, and produced two rows for one run."""
    for status in (RunStatus.RUNNING, RunStatus.RUNNING, RunStatus.SUCCEEDED):
        await store.set_status("r1", status)
    assert [r.run_id for r in await store.list_runs()] == ["r1"]


async def test_listing_is_newest_first_and_filterable(store):
    for i in range(3):
        await store.set_status(f"r{i}", RunStatus.RUNNING)
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
    await seed(store, "m1", model="mcmc")
    await seed(store, "s1", model="scenario")
    await seed(store, "m2", model="mcmc")

    assert {r.run_id for r in await store.list_runs(model="mcmc")} == {"m1", "m2"}
    assert [r.run_id for r in await store.list_runs(model="scenario")] == ["s1"]
    assert await store.list_runs(model="nothing-runs-this") == []


async def test_status_and_model_filters_combine_rather_than_override(store):
    """Two optional filters is where branch-per-filter starts producing the
    wrong SQL: the second filter quietly replaces the first."""
    await seed(store, "m1", model="mcmc")
    await seed(store, "m2", model="mcmc")
    await seed(store, "s1", model="scenario")
    await store.set_status("m2", RunStatus.SUCCEEDED)
    await store.set_status("s1", RunStatus.SUCCEEDED)

    both = await store.list_runs(status="SUCCEEDED", model="mcmc")
    assert [r.run_id for r in both] == ["m2"]


async def test_a_model_filter_is_a_bound_value_not_sql(store):
    await seed(store, "m1", model="mcmc")

    hostile = "'; DROP TABLE run_status; --"
    assert await store.list_runs(model=hostile) == []
    # The table is still there, which it would not be under interpolation.
    assert [r.run_id for r in await store.list_runs()] == ["m1"]


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
    await s.set_status("only-in-other-place", RunStatus.RUNNING)
    assert (await s.get("only-in-other-place")) is not None

    # And it really is a separate table, not the default one under a new name.
    default = PostgresRunStore(postgres)
    await default.ensure_schema()
    assert await default.get("only-in-other-place") is None


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
