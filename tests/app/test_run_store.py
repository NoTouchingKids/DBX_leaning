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
    qualified_history,
)
from shared.envelope import RunStatus, now_ms
from shared.tables import TableSet


def store_table() -> str:
    return qualified(DEFAULT_SCHEMA)


def history_table() -> str:
    return qualified_history(DEFAULT_SCHEMA)


async def append_transition(
    store,
    run_id: str,
    status: str,
    *,
    seq: int | None = None,
    ts: int | None = None,
    detail: str | None = None,
    recorded_by: str | None = None,
    ignore_duplicates: bool = False,
):
    """Insert one history row the way `job/lakebase.py` will.

    The writer is the job's, over the Database REST API, so there is nothing
    app-side to drive here — and a fake writer would only test the fake. This
    is the same INSERT, through psycopg, against the schema the app applies.

    `recorded_by` is left out of the statement when it is None, so the
    column's DEFAULT is what gets exercised rather than a value this helper
    supplied.

    Writes through `store._history` rather than the module-level helper, so a
    store on a non-default schema is appended to and read back in the same
    place — otherwise this inserts into `dbx_leaning` and the assertion reads
    an empty table somewhere else.
    """
    columns = ["run_id", "seq", "status", "detail", "ts"]
    values: list = [run_id, seq, status, detail, now_ms() if ts is None else ts]
    if recorded_by is not None:
        columns.append("recorded_by")
        values.append(recorded_by)
    placeholders = ", ".join(["%s"] * len(columns))
    conflict = " ON CONFLICT DO NOTHING" if ignore_duplicates else ""

    conn = await store._conn()
    try:
        await conn.execute(
            f"INSERT INTO {store._history} ({', '.join(columns)}) "
            f"VALUES ({placeholders}){conflict}",
            tuple(values),
        )
    finally:
        await conn.close()


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
        # Both tables: the Postgres server is module-scoped, so history rows
        # left behind would leak into the next test's reads.
        await conn.execute(f"TRUNCATE {store_table()}, {history_table()}")
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


# --- the append-only transition log ---------------------------------------
#
# `run_status` answers "what is this run doing"; `run_status_history` answers
# "how did it get there". Two tables rather than one append-only table,
# because appending to the current-state row costs the primary key on
# `run_id` and the transactional count-and-claim — the two properties this
# database was chosen for. The tests below hold both ends of that.


async def test_the_history_comes_back_oldest_first(store):
    await store.claim_slot("r1", model="mcmc", ceiling=5)
    for i, status in enumerate(("QUEUED", "RUNNING", "SUCCEEDED")):
        await append_transition(store, "r1", status, seq=i, ts=1_700_000_000_000 + i)

    assert [t.status for t in await store.history("r1")] == ["QUEUED", "RUNNING", "SUCCEEDED"]
    assert [t.seq for t in await store.history("r1")] == [0, 1, 2]


async def test_the_history_is_scoped_to_one_run(store):
    await append_transition(store, "r1", "RUNNING", seq=0)
    await append_transition(store, "r2", "FAILED", seq=0)

    assert [t.status for t in await store.history("r1")] == ["RUNNING"]
    assert [t.run_id for t in await store.history("r2")] == ["r2"]


async def test_an_unknown_run_has_an_empty_history(store):
    assert await store.history("never-existed") == []


async def test_a_re_reported_status_message_cannot_land_twice(store):
    """A job that reconnects re-reports; `seq` identifies the message.

    Without the unique index the same transition accumulates a row per retry,
    and a history that counts a run's restarts by counting its RUNNING rows
    starts lying. The writer says ON CONFLICT DO NOTHING because the database
    refuses the second one — this is that refusal.
    """
    import psycopg

    await append_transition(store, "r1", "RUNNING", seq=7)
    with pytest.raises(psycopg.errors.UniqueViolation):
        await append_transition(store, "r1", "RUNNING", seq=7)

    await append_transition(store, "r1", "RUNNING", seq=7, ignore_duplicates=True)
    assert len(await store.history("r1")) == 1

    # A different run reusing that seq is a different message. Per-run
    # counters mean seq 7 exists for every run there has ever been.
    await append_transition(store, "r2", "RUNNING", seq=7)
    assert len(await store.history("r2")) == 1


async def test_two_transitions_with_no_seq_can_both_be_appended(store):
    """Why the unique index is partial.

    The app appends at slot-claim time, where there is no envelope message and
    so no seq. Under a plain UNIQUE (run_id, seq) that would be one NULL row
    per run at most in any database that treats NULLs as equal, and this table
    has to tolerate more than one writer without a seq.
    """
    await append_transition(store, "r1", "QUEUED", recorded_by="app")
    await append_transition(store, "r1", "QUEUED", recorded_by="app")

    assert len(await store.history("r1")) == 2
    assert all(t.seq is None for t in await store.history("r1"))


async def test_the_writer_is_recorded_and_defaults_to_the_job(store):
    """Two writers on this table, and reading it later means knowing which one
    said what: the app has only its own view, the job has the run's."""
    await append_transition(store, "r1", "QUEUED", ts=1_000, recorded_by="app")
    await append_transition(store, "r1", "RUNNING", seq=3, ts=1_001)

    assert [(t.status, t.recorded_by) for t in await store.history("r1")] == [
        ("QUEUED", "app"),
        ("RUNNING", "job"),
    ]


async def test_transitions_inside_one_millisecond_keep_their_order(store):
    """`ts` is epoch milliseconds and milliseconds collide — QUEUED then
    RUNNING inside one is an ordinary fast start. Ordering on `ts` alone
    leaves the tie to the planner, and a history in the wrong order reads as
    a run that went backwards. The BIGSERIAL breaks it by insertion order."""
    for i, status in enumerate(("QUEUED", "RUNNING", "SUCCEEDED")):
        await append_transition(store, "r1", status, seq=i, ts=1_700_000_000_000)

    assert [t.status for t in await store.history("r1")] == ["QUEUED", "RUNNING", "SUCCEEDED"]
    ids = [t.id for t in await store.history("r1")]
    assert ids == sorted(ids)


async def test_the_limit_keeps_the_newest_transitions_not_the_oldest(store):
    """`ORDER BY ts LIMIT n` truncates the tail, which drops the terminal
    transition — the one row anyone reading a history wants. Newest-first in
    SQL, reversed on the way out."""
    for i in range(5):
        await append_transition(store, "r1", f"S{i}", seq=i, ts=1_000 + i)

    assert [t.status for t in await store.history("r1", limit=2)] == ["S3", "S4"]
    assert len(await store.history("r1")) == 5


async def test_seq_compares_as_a_number_not_a_string(store):
    """The lexicographic bug this repo hit twice, in schema form.

    `seq` is BIGINT. Were it TEXT, `seq > 9` would return 2 and not 12 —
    exactly inverted — because "2" > "9" and "12" is not.
    """
    for seq in (2, 12):
        await append_transition(store, "r1", "RUNNING", seq=seq, ts=1_000 + seq)

    conn = await store._conn()
    try:
        cur = await conn.execute(
            f"SELECT seq FROM {history_table()} WHERE run_id = %s AND seq > %s ORDER BY seq",
            ("r1", 9),
        )
        above_nine = [row[0] for row in await cur.fetchall()]
    finally:
        await conn.close()

    assert above_nine == [12]
    assert [t.seq for t in await store.history("r1")] == [2, 12]


async def test_an_unrecognised_status_is_kept_verbatim(store):
    """`RunRecord.from_row` maps an unknown status onto FAILED so the current
    state still renders. Doing that here would invent a transition that never
    happened and bury the data problem that produced it."""
    await append_transition(store, "r1", "WEDGED", seq=0)

    assert [t.status for t in await store.history("r1")] == ["WEDGED"]


async def test_the_detail_survives_the_round_trip(store):
    await append_transition(store, "r1", "FAILED", seq=4, detail="solver ran out of memory")

    transition = (await store.history("r1"))[0]
    assert transition.detail == "solver ran out of memory"
    assert (await store.history("r1"))[0].ts > 0


async def test_the_history_does_not_make_run_status_append_only(store):
    """The invariant the split exists to protect.

    Three transitions on one run leave three history rows and exactly ONE
    `run_status` row. Lose that and `ON CONFLICT (run_id) DO UPDATE` has no
    target, a duplicate run_id stops being refusable, and the ceiling count
    becomes a latest-row-per-run subquery.
    """
    await store.claim_slot("r1", model="mcmc", ceiling=5)
    for i, status in enumerate((RunStatus.RUNNING, RunStatus.SUCCEEDED)):
        await store.set_status("r1", status)
        await append_transition(store, "r1", status.value, seq=i, ts=1_000 + i)

    conn = await store._conn()
    try:
        cur = await conn.execute(
            f"SELECT COUNT(*) FROM {store_table()} WHERE run_id = %s", ("r1",)
        )
        rows = int((await cur.fetchone())[0])
    finally:
        await conn.close()

    assert rows == 1, "run_status must stay one row per run"
    assert len(await store.history("r1")) == 2
    assert await store.active_count() == 0, "the terminal run still frees its slot"

    with pytest.raises(DuplicateRun):
        await store.claim_slot("r1", model="mcmc", ceiling=5)


async def test_applying_the_schema_again_keeps_the_history(store):
    """`ensure_schema()` runs on every startup, and an app restart is routine
    — up to 24h, in practice ~8. IF NOT EXISTS on the table and on both
    indexes is what makes the second run a no-op rather than an error or, far
    worse, a reset."""
    await append_transition(store, "r1", "RUNNING", seq=1)
    await store.ensure_schema()
    await store.ensure_schema()

    assert [t.status for t in await store.history("r1")] == ["RUNNING"]


async def test_the_warehouse_store_actually_stores_the_job_run_id():
    """`attach_job_run` on the warehouse path wrote nothing.

    `claim_slot` inserts the row before the Jobs API is called, so by the time
    a job run id exists the row does too — and `set_run_status`'s MERGE set
    `job_run_id` only on its NOT MATCHED branch. The column stayed NULL
    forever, and `app/server/reconcile.py::_resolve` needs it to ask the Jobs API how
    a run ended. Without it that route is dead and reconciliation degrades to
    the last `run_events` row, which is precisely what a job that crashed
    before emitting anything never wrote.

    Nothing failed, and nothing said so. Same quiet class as the Lakebase
    wiring gap.
    """
    from server.repository import RunRepository
    from server.store import WarehouseRunStore

    sql = RecordingSql()
    store = WarehouseRunStore(RunRepository(sql, TableSet()))

    await store.attach_job_run("r1", 4242)

    merge = next(q for q, _ in sql.queries if "MERGE INTO" in q)
    matched = merge.split("WHEN MATCHED")[1].split("WHEN NOT MATCHED")[0]
    assert "job_run_id" in matched, (
        "the MATCHED branch never assigns job_run_id, so attach_job_run is a no-op"
    )
    params = next(p for q, p in sql.queries if "MERGE INTO" in q)
    assert any(getattr(x, "value", None) == "4242" for x in params)


async def test_a_status_update_does_not_wipe_the_job_run_id():
    """Every ordinary transition calls `set_run_status` with `job_run_id=None`.

    A plain `t.job_run_id = :job_run_id` on the MATCHED branch would fix the
    bug above and immediately introduce a worse one: the id would be nulled on
    the first RUNNING write, one message after it was stored. Hence COALESCE.
    """
    from server.repository import RunRepository

    sql = RecordingSql()
    repo = RunRepository(sql, TableSet())

    await repo.set_run_status("r1", "RUNNING")

    merge = next(q for q, _ in sql.queries if "MERGE INTO" in q)
    matched = merge.split("WHEN MATCHED")[1].split("WHEN NOT MATCHED")[0]
    assert "COALESCE" in matched.upper(), "a null job_run_id must not overwrite a stored one"


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

    # Membership, not equality. The Postgres server is module-scoped and other
    # tests here deliberately build stores on other schemas, so an equality
    # assertion fails the moment one of them is reordered above this — an
    # afternoon lost to a failure that says nothing about `public`, which is
    # the only thing this test is actually about.
    assert "public" not in schemas, f"run_status landed in public: {schemas}"
    assert DEFAULT_SCHEMA in schemas, f"run_status landed in {schemas}, not {DEFAULT_SCHEMA}"


async def test_the_history_table_is_not_in_public_either(postgres):
    """Same failure as `run_status`: `public` grants no CREATE to a non-owner
    since PostgreSQL 15, and an unqualified name would find nothing."""
    s = PostgresRunStore(postgres)
    await s.ensure_schema()

    conn = await s._conn()
    try:
        cur = await conn.execute(
            "SELECT schemaname FROM pg_tables WHERE tablename = 'run_status_history'"
        )
        schemas = [row[0] for row in await cur.fetchall()]
    finally:
        await conn.close()

    assert "public" not in schemas, f"run_status_history landed in public: {schemas}"
    assert DEFAULT_SCHEMA in schemas


async def test_a_custom_schema_is_honoured(postgres):
    s = PostgresRunStore(postgres, schema="other_place")
    await s.ensure_schema()
    await s.claim_slot("r1", model="scenario", ceiling=5)
    assert await s.active_count() == 1

    # And it really is a separate table, not the default one under a new name.
    default = PostgresRunStore(postgres)
    await default.ensure_schema()
    assert await default.active_count() == 0


async def test_a_custom_schema_gets_its_own_history_table(postgres):
    """The history follows the schema setting. A qualifier built once for
    `run_status` and forgotten for the second table would leave a relocated
    deployment appending transitions into the default schema — which on
    Lakebase is a schema the role may not even own."""
    s = PostgresRunStore(postgres, schema="other_place")
    await s.ensure_schema()
    assert s._history == "other_place.run_status_history"

    # A run id nothing else in this module uses: this test does not take the
    # truncating fixture, so the default schema still holds whatever the last
    # test that did left behind.
    await append_transition(s, "custom-schema-run", "RUNNING", seq=0)
    assert [t.status for t in await s.history("custom-schema-run")] == ["RUNNING"]

    default = PostgresRunStore(postgres)
    await default.ensure_schema()
    assert await default.history("custom-schema-run") == []


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
