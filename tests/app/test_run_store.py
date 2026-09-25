"""The run store, driven against a real Postgres.

Lakebase is standard Postgres, so everything here except how the password is
obtained behaves identically to the real thing. Tested against PostgreSQL 16,
which is what this environment provides AND what the Lakebase instance created
on 2026-08-25 came back as — the CLI ignores `pg_version` on create, so 18 is
reachable only through the workspace UI. Nothing used here — primary keys,
ON CONFLICT ... WHERE, data-modifying CTEs, partial indexes — differs between
16 and 18, but that is a claim about the feature set, not a test result. The
app asserts neither: `check_schema()` reads `SHOW server_version` and
`/healthz` reports what the server actually said.

**The app creates nothing any more**, so these tests apply the committed
`lakebase_ddl/*.sql` files themselves — the same files a human applies with
psql. That makes every test here a test of those files as well as of the
store, and `test_the_committed_ddl_passes_the_startup_check` is the drift test
between them and `store.EXPECTED_COLUMNS`.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest
from fastapi.testclient import TestClient

from server.store import (
    DEFAULT_SCHEMA,
    HISTORY_TABLE,
    PostgresRunStore,
    UnsafeSchemaName,
    qualified,
)
from shared.envelope import RunStatus

pgserver = pytest.importorskip("pgserver", reason="needs the dev group")

DDL_DIR = pathlib.Path(__file__).resolve().parents[2] / "lakebase_ddl"
DDL_FILES = sorted(DDL_DIR.glob("*.sql"))


async def apply_ddl(uri: str, schema: str = DEFAULT_SCHEMA) -> None:
    """What `deploy/README.md` tells a human to do with psql, in order.

    A non-default schema is the files with the name substituted — the same
    thing the files' headers tell an operator to do.
    """
    import psycopg

    async with await psycopg.AsyncConnection.connect(uri, autocommit=True) as conn:
        for path in DDL_FILES:
            text = path.read_text()
            if schema != DEFAULT_SCHEMA:
                text = text.replace(f"{DEFAULT_SCHEMA}.", f"{schema}.").replace(
                    f"EXISTS {DEFAULT_SCHEMA};", f"EXISTS {schema};"
                )
            await conn.execute(text)


async def execute(uri: str, sql: str, params=None):
    import psycopg

    async with await psycopg.AsyncConnection.connect(uri, autocommit=True) as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchall() if cur.description else None


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
    await apply_ddl(postgres)
    # Qualified, like every statement the store itself issues. An unqualified
    # name resolves through `search_path` to a `public` table that does not
    # exist — the exact failure the schema exists to prevent.
    await execute(
        postgres,
        f"TRUNCATE {qualified(DEFAULT_SCHEMA)}, {qualified(DEFAULT_SCHEMA, HISTORY_TABLE)}",
    )
    return PostgresRunStore(postgres)


async def report(
    store: PostgresRunStore,
    run_id: str,
    status: str,
    seq: int,
    *,
    terminal: bool = False,
    detail: str | None = None,
    model: str = "",
    job_run_id: str | None = None,
    ts: int | None = None,
) -> None:
    await store.set_status(
        run_id,
        status,
        seq=seq,
        terminal=terminal,
        ts=1_000 + seq if ts is None else ts,
        detail=detail,
        model=model,
        job_run_id=job_run_id,
    )


# --- the schema: checked, never created ------------------------------------


async def test_the_committed_ddl_passes_the_startup_check(store):
    """The drift test between `lakebase_ddl/*.sql` and `EXPECTED_COLUMNS`, run
    for real: the files applied, then the app's own check read back."""
    assert await store.check_schema() == []


async def test_the_check_is_repeatable_and_changes_nothing(store):
    await report(store, "r1", RunStatus.RUNNING, 1)
    assert await store.check_schema() == []
    assert await store.check_schema() == []
    assert [r.run_id for r in await store.list_runs()] == ["r1"]


async def test_missing_tables_are_reported_not_created(postgres):
    """Phase 2 item 3: the app reports a missing table; it does not make one."""
    s = PostgresRunStore(postgres, schema="never_applied")
    problems = await s.check_schema()

    assert len(problems) == 2, problems
    assert "never_applied.run_status does not exist" in problems[0]
    assert "001_run_status.sql" in problems[0]
    assert "never_applied.run_status_history does not exist" in problems[1]
    assert "002_run_status_history.sql" in problems[1]

    rows = await execute(
        postgres,
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'never_applied'",
    )
    assert rows == [(0,)], "check_schema created something"
    rows = await execute(
        postgres,
        "SELECT count(*) FROM information_schema.schemata WHERE schema_name = 'never_applied'",
    )
    assert rows == [(0,)], "check_schema created the schema"


async def test_a_pre_v5_run_status_is_reported_as_mismatched(postgres):
    """CREATE TABLE IF NOT EXISTS leaves an old table alone, so a deployment
    that applied v4's 001 and then this one still has v4's shape. The check is
    what notices."""
    await execute(postgres, "CREATE SCHEMA IF NOT EXISTS old_shape")
    await execute(
        postgres,
        """
        CREATE TABLE old_shape.run_status (
            run_id TEXT PRIMARY KEY, job_run_id TEXT, model TEXT NOT NULL,
            status TEXT NOT NULL, detail TEXT, started_ts BIGINT NOT NULL,
            updated_ts BIGINT NOT NULL, requested_by TEXT
        )
        """,
    )
    problems = await PostgresRunStore(postgres, schema="old_shape").check_schema()
    text = "\n".join(problems)
    for column in ("terminal", "seq", "updated_at"):
        assert f"old_shape.run_status has no column {column}" in text
    assert "old_shape.run_status_history does not exist" in text


async def test_a_wrong_type_is_reported(postgres):
    """`seq` as TEXT is the lexicographic-comparison bug waiting to happen."""
    await apply_ddl(postgres, schema="wrong_type")
    await execute(postgres, "ALTER TABLE wrong_type.run_status ALTER COLUMN seq TYPE text")
    problems = await PostgresRunStore(postgres, schema="wrong_type").check_schema()
    assert problems == ["wrong_type.run_status.seq is text NOT NULL, expected bigint NOT NULL"]


async def test_a_missing_conflict_target_is_reported(postgres):
    """Right columns, no unique key: every write would fail on ON CONFLICT."""
    await apply_ddl(postgres, schema="no_key")
    await execute(
        postgres,
        "ALTER TABLE no_key.run_status_history DROP CONSTRAINT run_status_history_run_seq_key",
    )
    problems = await PostgresRunStore(postgres, schema="no_key").check_schema()
    assert len(problems) == 1 and "(run_id, seq)" in problems[0], problems


async def test_an_extra_column_is_tolerated(postgres):
    """Adding a column out of band ahead of the app is an additive migration."""
    await apply_ddl(postgres, schema="extra_col")
    await execute(postgres, "ALTER TABLE extra_col.run_status ADD COLUMN note TEXT")
    assert await PostgresRunStore(postgres, schema="extra_col").check_schema() == []


# --- the seq guard ---------------------------------------------------------


async def test_status_transitions_are_recorded(store):
    await report(store, "r1", RunStatus.RUNNING, 1, model="mcmc")
    await report(store, "r1", "SUCCEEDED", 7, terminal=True, detail="all draws done")

    record = await store.get("r1")
    assert record.status == RunStatus.SUCCEEDED and record.detail == "all draws done"
    assert record.terminal and record.seq == 7 and record.updated_at == 1_007
    assert record.model == "mcmc", "a status write must not clobber the model"


async def test_a_late_lower_seq_does_not_move_the_row_backwards(store):
    await report(store, "r1", RunStatus.RUNNING, 1)
    await report(store, "r1", RunStatus.SUCCEEDED, 5, terminal=True)
    # RUNNING arrives late — a slow write, a reconnect.
    await report(store, "r1", RunStatus.RUNNING, 3, detail="late")

    record = await store.get("r1")
    assert (record.status, record.terminal, record.seq) == (RunStatus.SUCCEEDED, True, 5)
    assert record.detail is None


async def test_the_guard_compares_seq_as_a_number_not_a_string(store):
    """Regression for the lexicographic bug this repo hit twice: as strings,
    "2" > "12", and a stale seq 2 would overwrite seq 12."""
    await report(store, "r1", RunStatus.SUCCEEDED, 12, terminal=True)
    await report(store, "r1", RunStatus.RUNNING, 2)
    assert (await store.get("r1")).seq == 12


async def test_the_guard_uses_seq_not_the_clock(store):
    """A late report carries a LATER ts; only seq says it is stale."""
    await report(store, "r1", RunStatus.SUCCEEDED, 5, terminal=True, ts=100)
    await report(store, "r1", RunStatus.RUNNING, 4, ts=999_999)
    record = await store.get("r1")
    assert record.status == RunStatus.SUCCEEDED and record.updated_at == 100


async def test_an_equal_seq_is_idempotent(store):
    for _ in range(3):
        await report(store, "r1", RunStatus.SUCCEEDED, 4, terminal=True, detail="done")
    record = await store.get("r1")
    assert (record.status, record.seq, record.detail) == (RunStatus.SUCCEEDED, 4, "done")
    assert [h.seq for h in await store.history("r1")] == [4]


async def test_terminal_is_stored_as_reported_not_derived(store):
    """A model-defined status can be terminal, and a platform status string is
    not terminal just because of its spelling: the producer says which."""
    await report(store, "custom", "CONVERGED_EARLY", 3, terminal=True)
    await report(store, "odd", "SUCCEEDED", 3, terminal=False)
    assert (await store.get("custom")).terminal is True
    assert (await store.get("odd")).terminal is False


# --- model and job_run_id are never blanked ---------------------------------


async def test_a_later_write_never_blanks_a_known_model_or_job_run_id(store):
    await report(store, "r1", RunStatus.RUNNING, 1, model="mcmc", job_run_id="998877")
    # The app's writer knows neither: '' and None.
    await report(store, "r1", RunStatus.SUCCEEDED, 2, terminal=True)
    record = await store.get("r1")
    assert (record.model, record.job_run_id) == ("mcmc", "998877")

    await report(store, "r1", RunStatus.SUCCEEDED, 3, terminal=True, job_run_id="")
    assert (await store.get("r1")).job_run_id == "998877", "'' must not blank it either"


async def test_a_later_write_can_fill_in_what_an_earlier_one_did_not_know(store):
    await report(store, "r1", RunStatus.RUNNING, 1)
    assert (await store.get("r1")).model == ""
    await report(store, "r1", RunStatus.SUCCEEDED, 2, terminal=True, model="mcmc")
    assert (await store.get("r1")).model == "mcmc"


# --- history ---------------------------------------------------------------


async def test_history_records_every_transition_in_seq_order(store):
    await report(store, "r1", RunStatus.RUNNING, 1)
    await report(store, "r1", "PHASE_2", 4, detail="warm start")
    await report(store, "r1", RunStatus.SUCCEEDED, 9, terminal=True)

    history = await store.history("r1")
    assert [(h.seq, h.status, h.terminal) for h in history] == [
        (1, "RUNNING", False),
        (4, "PHASE_2", False),
        (9, "SUCCEEDED", True),
    ]
    assert history[1].detail == "warm start" and history[1].ts == 1_004


async def test_history_appends_even_when_the_guard_refuses_the_upsert(store):
    """Current state is what is true; history is what was reported."""
    await report(store, "r1", RunStatus.SUCCEEDED, 5, terminal=True)
    await report(store, "r1", RunStatus.RUNNING, 3, detail="late")

    assert (await store.get("r1")).status == RunStatus.SUCCEEDED
    history = await store.history("r1")
    assert [(h.seq, h.status) for h in history] == [(3, "RUNNING"), (5, "SUCCEEDED")]


async def test_a_redelivered_report_is_one_history_row(store):
    """A reconnect or a retry may deliver the same status message twice; its
    (run_id, seq) identifies it."""
    await report(store, "r1", RunStatus.RUNNING, 1)
    await report(store, "r1", RunStatus.RUNNING, 1)
    await report(store, "r1", RunStatus.RUNNING, 1)
    assert len(await store.history("r1")) == 1


async def test_history_is_per_run_and_empty_for_an_unknown_one(store):
    await report(store, "a", RunStatus.RUNNING, 1)
    await report(store, "b", RunStatus.RUNNING, 1)
    assert [h.run_id for h in await store.history("a")] == ["a"]
    assert await store.history("never-reported") == []


# --- rows and listing ------------------------------------------------------


async def test_a_status_for_an_unknown_run_creates_it(store):
    """A job can start while the app is down; its first status message may be
    the app's first sight of the run."""
    await report(store, "appeared-from-nowhere", RunStatus.RUNNING, 1)
    assert (await store.get("appeared-from-nowhere")).status == RunStatus.RUNNING


async def test_a_run_id_is_one_row_however_often_it_is_written(store):
    """The primary key is the property Postgres was chosen for: Delta had
    none, and produced two rows for one run."""
    for seq, status in enumerate((RunStatus.RUNNING, RunStatus.RUNNING, RunStatus.SUCCEEDED)):
        await report(store, "r1", status, seq)
    assert [r.run_id for r in await store.list_runs()] == ["r1"]


async def test_listing_is_newest_first_and_filterable(store):
    for i in range(3):
        await report(store, f"r{i}", RunStatus.RUNNING, 1, ts=100 + i)
    await report(store, "r0", RunStatus.SUCCEEDED, 2, terminal=True, ts=500)

    everything = await store.list_runs(limit=10)
    assert [r.run_id for r in everything] == ["r0", "r2", "r1"]

    succeeded = await store.list_runs(status="SUCCEEDED")
    assert [r.run_id for r in succeeded] == ["r0"]
    assert len(await store.list_runs(limit=2)) == 2


async def test_listing_can_be_filtered_by_model(store):
    await report(store, "m1", RunStatus.RUNNING, 1, model="mcmc")
    await report(store, "s1", RunStatus.RUNNING, 1, model="scenario")
    await report(store, "m2", RunStatus.RUNNING, 1, model="mcmc")

    assert {r.run_id for r in await store.list_runs(model="mcmc")} == {"m1", "m2"}
    assert [r.run_id for r in await store.list_runs(model="scenario")] == ["s1"]
    assert await store.list_runs(model="nothing-runs-this") == []


async def test_status_and_model_filters_combine_rather_than_override(store):
    """Two optional filters is where branch-per-filter starts producing the
    wrong SQL: the second filter quietly replaces the first."""
    await report(store, "m1", RunStatus.RUNNING, 1, model="mcmc")
    await report(store, "m2", RunStatus.SUCCEEDED, 2, terminal=True, model="mcmc")
    await report(store, "s1", RunStatus.SUCCEEDED, 2, terminal=True, model="scenario")

    both = await store.list_runs(status="SUCCEEDED", model="mcmc")
    assert [r.run_id for r in both] == ["m2"]


async def test_a_model_filter_is_a_bound_value_not_sql(store):
    await report(store, "m1", RunStatus.RUNNING, 1, model="mcmc")

    hostile = "'; DROP TABLE run_status; --"
    assert await store.list_runs(model=hostile) == []
    # The table is still there, which it would not be under interpolation.
    assert [r.run_id for r in await store.list_runs()] == ["m1"]


async def test_an_unknown_run_is_none_not_an_error(store):
    assert await store.get("never-existed") is None


async def test_a_record_serialises_the_signed_off_columns(store):
    await report(store, "r1", RunStatus.RUNNING, 1, model="mcmc", job_run_id="42")
    assert (await store.get("r1")).as_dict() == {
        "run_id": "r1",
        "model": "mcmc",
        "status": "RUNNING",
        "terminal": False,
        "seq": 1,
        "updated_at": 1_001,
        "job_run_id": "42",
        "detail": None,
    }


# --- server version and credentials ----------------------------------------


async def test_the_store_reports_the_postgres_version_it_actually_got(store):
    """This repo once asserted "Lakebase runs PostgreSQL 18"; a real instance
    came back `PG_VERSION_16`, which is the default. This asserts the answer is
    populated and looks like a version, not that it equals any particular one.
    """
    await store.check_schema()
    assert store.server_version is not None
    assert store.server_version[0].isdigit(), store.server_version


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

    Two operations, two resolutions, and the second one gets the newer token.
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

                async def fetchall(self):
                    return []

            return Cur()

        async def close(self):
            pass

    async def connect():
        # Mirrors what _conn does for real: resolve, then hand the value over.
        await provider()
        return FakeConn()

    store = PostgresRunStore("postgresql://pg/db", password_provider=provider, connect=connect)
    await store.check_schema()
    await store.check_schema()
    assert seen == ["tok-1", "tok-2"], "the credential was reused across connections"


async def test_no_provider_means_the_dsn_is_used_unchanged():
    """The local dev stack has no auth at all, and an instance with
    `enable_pg_native_login` on has a real password in the DSN."""
    store = PostgresRunStore("postgresql://pg/db")
    assert store._password_provider is None


# --- the schema the tables live in -----------------------------------------


async def test_the_tables_are_not_in_public(store, postgres):
    """`public` grants no CREATE to non-owners since PostgreSQL 15."""
    rows = await execute(
        postgres,
        "SELECT DISTINCT schemaname FROM pg_tables "
        "WHERE tablename IN ('run_status', 'run_status_history')",
    )
    assert DEFAULT_SCHEMA in {r[0] for r in rows}
    assert "public" not in {r[0] for r in rows}


async def test_a_custom_schema_is_honoured(postgres):
    await apply_ddl(postgres, schema="other_place")
    s = PostgresRunStore(postgres, schema="other_place")
    assert await s.check_schema() == []
    await report(s, "only-in-other-place", RunStatus.RUNNING, 1)
    assert (await s.get("only-in-other-place")) is not None

    # And it really is a separate table, not the default one under a new name.
    await apply_ddl(postgres)
    assert await PostgresRunStore(postgres).get("only-in-other-place") is None


@pytest.mark.parametrize(
    "bad",
    ["public; DROP TABLE run_status", "has-a-hyphen", "1_starts_with_digit", "", "a b"],
)
def test_a_schema_name_that_is_not_an_identifier_is_refused(bad):
    """The schema reaches SQL by interpolation because an identifier cannot be
    a bound parameter. Refused at construction, so a bad value fails while the
    app is starting and can report it."""
    with pytest.raises(UnsafeSchemaName):
        PostgresRunStore("postgresql://pg/db", schema=bad)


# --- startup and /healthz --------------------------------------------------


async def test_startup_reports_a_missing_table_as_degraded_instead_of_creating_it(
    postgres, app_and_hub, config
):
    cfg = config(lakebase_dsn=postgres, lakebase_schema="startup_unapplied")
    application, hub = app_and_hub(cfg)
    await hub._start_store(cfg)

    assert hub.store is None
    reason = hub.degraded["lakebase_schema"]
    assert "startup_unapplied.run_status does not exist" in reason
    assert "001_run_status.sql" in reason and "002_run_status_history.sql" in reason

    rows = await execute(
        postgres,
        "SELECT count(*) FROM information_schema.schemata WHERE schema_name = 'startup_unapplied'",
    )
    assert rows == [(0,)], "startup created the schema"

    client = TestClient(application)
    health = client.get("/api/healthz").json()
    assert health["status"] == "degraded"
    assert health["degraded"]["lakebase_schema"] == reason
    # And a route needing the store is a clean 503 carrying the same reason.
    listing = client.get("/api/runs")
    assert listing.status_code == 503
    assert "does not exist" in listing.json()["detail"]


async def test_startup_attaches_the_store_when_the_schema_checks_out(postgres, app_and_hub, config):
    await apply_ddl(postgres, schema="startup_applied")
    cfg = config(lakebase_dsn=postgres, lakebase_schema="startup_applied")
    application, hub = app_and_hub(cfg)
    await hub._start_store(cfg)

    assert hub.store is not None
    assert not {"lakebase", "lakebase_schema", "store"} & set(hub.degraded)
    health = TestClient(application).get("/api/healthz").json()
    assert health["store"]["kind"] == "postgres"
    assert health["store"]["server_version"]


async def test_the_app_never_writes_run_status_it_only_reads_it(postgres, app_and_hub, config):
    """v5 Phase 2 item 5: the job is the sole writer of `run_status`.

    An ingested status message is a notification for the SSE stream and
    writes nothing — two writers to one row is the bug Phase 2 exists to
    remove. The rows the job writes (`job/lakebase.py`, stood in for here by
    the same shared statement via `store.set_status`) are what the listing and
    history routes serve.
    """
    from shared.envelope import StatusMessage

    await apply_ddl(postgres, schema="ingest_path")
    cfg = config(lakebase_dsn=postgres, lakebase_schema="ingest_path")
    application, hub = app_and_hub(cfg)
    await hub._start_store(cfg)

    await hub.ingest("r1", StatusMessage(run_id="r1", seq=3, ts=77, status="RUNNING"))
    assert await hub.store.get("r1") is None, "the app wrote run_status"

    await hub.store.set_status("r1", "RUNNING", seq=3, terminal=False, ts=77)
    await hub.store.set_status("r1", "INFEASIBLE", seq=9, terminal=True, ts=88)

    record = await hub.store.get("r1")
    assert (record.status, record.terminal, record.seq, record.updated_at) == (
        "INFEASIBLE",
        True,
        9,
        88,
    )
    client = TestClient(application)
    body = client.get("/api/runs/r1/history").json()
    assert [(t["seq"], t["status"]) for t in body["transitions"]] == [
        (3, "RUNNING"),
        (9, "INFEASIBLE"),
    ]
