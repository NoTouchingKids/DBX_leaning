"""The job reporting its own status to Lakebase, over a direct Postgres
connection.

`run_status` used to be maintained only by the app, from messages arriving
over the socket — which made a fact about the run depend on the observer
being up. The job knows its own status, so it reports it.

This used to connect over the Database REST API and these tests asserted on
the JSON body a fake HTTP client received. That endpoint turned out to be a
PostgREST base, which does not accept raw SQL — the requests here would never
have landed against a real instance, however carefully the JSON matched.
`job/lakebase.py` now speaks Postgres directly, so these tests run `REPORT_SQL`
and `LakebaseStatus` against a REAL PostgreSQL 16 (`pgserver`, the same
embedded server `tests/app/test_run_store.py` uses) rather than asserting on
strings alone — the point of the rewrite is a statement that actually
behaves, and a fake client cannot tell that apart from one that does not.

Two layers:

- **`REPORT_SQL`'s own semantics** (fresh run, later transition, a stale one,
  a redelivered `(run_id, seq)`, a NULL seq, `requested_by` surviving,
  `COALESCE(NULLIF(...))` filling in an empty model) — run directly against a
  real schema, with full control over every column. This is the one thing
  `RunRecord.summary()` cannot exercise on its own: `updated_ts` is always
  stamped from the wall clock at call time, so it only ever increases from one
  report to the next — there is no way to construct a stale write through the
  public API. `tests/app/test_run_store.py::report_as_the_job_would` runs the
  same statement from the app's side, against the same table, so the two
  writers' agreement about which direction is backwards is pinned there; this
  file is about the statement's OWN behaviour in isolation.
- **`LakebaseStatus`**, the class: report() wiring `RunRecord.summary()`
  through correctly, counters, and every degrade path — unconfigured, no
  credential, an unreachable host, a role that cannot authenticate. Nothing
  here is load-bearing: unconfigured, refused or exploding, the run carries on
  and `run_events` on the durable path still carries what happened.
"""

from __future__ import annotations

import getpass
import pathlib
import re
import ssl
import tempfile

import pytest

from job.auth import AppCredential
from job.lakebase import REPORT_SQL, LakebaseStatus, connect_kwargs
from job.record import RunRecord
from job.shared.envelope import make_message

pgserver = pytest.importorskip("pgserver", reason="needs the dev group")

from server.store import history_schema_sql, schema_sql  # noqa: E402

SCHEMA = "dbx_leaning"
TABLE = f"{SCHEMA}.run_status"
HISTORY = f"{SCHEMA}.run_status_history"


def record_at(
    status: str, detail: str | None = None, *, seq: int = 0, ts: int | None = None
) -> RunRecord:
    record = RunRecord("run-1", model="scenario", job_run_id="jr-7")
    record.observe(
        make_message("status", run_id="run-1", seq=seq, ts=ts, status=status, detail=detail)
    )
    return record


def base_params(run_id: str = "r1", **overrides) -> dict:
    params = {
        "run_id": run_id,
        "job_run_id": "jr-1",
        "model": "scenario",
        "status": "RUNNING",
        "detail": None,
        "started_ts": 1_000,
        "updated_ts": 1_000,
        "seq": 0,
        "ts": 1_000,
    }
    params.update(overrides)
    return params


async def report_sql(dsn: str, **params) -> None:
    """Run `REPORT_SQL` directly, with full control over every column —
    including `updated_ts`, which `RunRecord.summary()` always stamps from the
    wall clock and so can never go backwards through the public API. This is
    what lets the stale-transition test below exist at all.
    """
    import psycopg

    conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    try:
        await conn.execute(REPORT_SQL.format(schema=SCHEMA), params)
    finally:
        await conn.close()


async def current_row(dsn: str, run_id: str) -> dict | None:
    import psycopg

    conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    try:
        cur = await conn.execute(
            f"SELECT status, detail, model, job_run_id, requested_by, started_ts, updated_ts "
            f"FROM {TABLE} WHERE run_id = %s",
            (run_id,),
        )
        row = await cur.fetchone()
    finally:
        await conn.close()
    if row is None:
        return None
    names = ("status", "detail", "model", "job_run_id", "requested_by", "started_ts", "updated_ts")
    return dict(zip(names, row, strict=True))


async def history_rows(dsn: str, run_id: str) -> list[dict]:
    import psycopg

    conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    try:
        cur = await conn.execute(
            f"SELECT seq, status, detail, ts, recorded_by FROM {HISTORY} "
            f"WHERE run_id = %s ORDER BY id",
            (run_id,),
        )
        rows = await cur.fetchall()
    finally:
        await conn.close()
    names = ("seq", "status", "detail", "ts", "recorded_by")
    return [dict(zip(names, r, strict=True)) for r in rows]


@pytest.fixture(scope="module")
def postgres():
    directory = pathlib.Path(tempfile.mkdtemp()) / "pg"
    server = pgserver.get_server(directory)
    try:
        yield server.get_uri()
    finally:
        server.cleanup()


@pytest.fixture
async def lakebase_dsn(postgres):
    """A clean schema for every test. Module-scoped Postgres, function-scoped
    truncate — standing up embedded Postgres per test is slow, and a run_id
    left over from a previous test's assertions would leak into the next's.
    """
    import psycopg

    conn = await psycopg.AsyncConnection.connect(postgres, autocommit=True)
    try:
        await conn.execute(schema_sql(SCHEMA) + history_schema_sql(SCHEMA))
        await conn.execute(f"TRUNCATE {TABLE}, {HISTORY}")
    finally:
        await conn.close()
    return postgres


# --- REPORT_SQL's own semantics, against a real PostgreSQL 16 --------------


async def test_a_fresh_run_inserts_both_rows(lakebase_dsn):
    await report_sql(lakebase_dsn, **base_params(status="QUEUED", seq=None))

    assert (await current_row(lakebase_dsn, "r1"))["status"] == "QUEUED"
    assert len(await history_rows(lakebase_dsn, "r1")) == 1


async def test_a_later_transition_updates_current_state_and_appends_history(lakebase_dsn):
    await report_sql(
        lakebase_dsn, **base_params(status="QUEUED", seq=0, ts=1_000, updated_ts=1_000)
    )
    await report_sql(
        lakebase_dsn, **base_params(status="RUNNING", seq=1, ts=2_000, updated_ts=2_000)
    )

    row = await current_row(lakebase_dsn, "r1")
    assert row["status"] == "RUNNING" and row["updated_ts"] == 2_000
    assert [h["status"] for h in await history_rows(lakebase_dsn, "r1")] == ["QUEUED", "RUNNING"]


async def test_a_stale_transition_leaves_current_state_untouched_but_still_appends_to_history(
    lakebase_dsn,
):
    """The two tables answer different questions. Current state is what is
    true, so a stale RUNNING arriving after SUCCEEDED must not move it
    backwards. History is what was *reported*, and the stale transition
    having arrived at all is the fact you want when working out why the row
    looks the way it does.
    """
    await report_sql(
        lakebase_dsn, **base_params(status="RUNNING", seq=0, ts=1_000, updated_ts=1_000)
    )
    await report_sql(
        lakebase_dsn, **base_params(status="SUCCEEDED", seq=1, ts=3_000, updated_ts=3_000)
    )

    await report_sql(lakebase_dsn, **base_params(status="STALE", seq=2, ts=2_000, updated_ts=2_000))

    row = await current_row(lakebase_dsn, "r1")
    assert row["status"] == "SUCCEEDED" and row["updated_ts"] == 3_000, (
        "the guard must refuse a write carrying an earlier updated_ts"
    )
    assert [h["status"] for h in await history_rows(lakebase_dsn, "r1")] == [
        "RUNNING",
        "SUCCEEDED",
        "STALE",
    ], "the stale write must still be recorded, even though it did not take"


async def test_a_redelivered_run_id_and_seq_appends_nothing_the_second_time(lakebase_dsn):
    """A job that reconnects re-reports; `seq` identifies the message.
    Without the partial unique index this accumulates a row per retry."""
    params = base_params(status="RUNNING", seq=5, ts=1_000, updated_ts=1_000)
    await report_sql(lakebase_dsn, **params)
    await report_sql(lakebase_dsn, **params)

    assert len(await history_rows(lakebase_dsn, "r1")) == 1


async def test_a_different_run_reusing_the_same_seq_is_a_different_message(lakebase_dsn):
    """Per-run counters mean seq 5 exists once for every run there has ever
    been — the unique index is on (run_id, seq), not seq alone."""
    await report_sql(lakebase_dsn, **base_params(run_id="r1", status="RUNNING", seq=5))
    await report_sql(lakebase_dsn, **base_params(run_id="r2", status="RUNNING", seq=5))

    assert len(await history_rows(lakebase_dsn, "r1")) == 1
    assert len(await history_rows(lakebase_dsn, "r2")) == 1


async def test_a_null_seq_appends_every_time(lakebase_dsn):
    """A report made before any status message exists has no message identity
    to dedupe by, and must still be able to append every time."""
    await report_sql(lakebase_dsn, **base_params(status="X1", seq=None, ts=1_000, updated_ts=1_000))
    await report_sql(lakebase_dsn, **base_params(status="X2", seq=None, ts=1_001, updated_ts=1_001))

    rows = await history_rows(lakebase_dsn, "r1")
    assert [r["status"] for r in rows] == ["X1", "X2"]
    assert all(r["seq"] is None for r in rows)


async def test_requested_by_written_by_the_app_survives_the_jobs_upsert(lakebase_dsn):
    """`requested_by` is the app's column: the job's upsert does not bind it
    (see `REPORT_SQL`'s own comment), so it must ride through untouched."""
    import psycopg

    conn = await psycopg.AsyncConnection.connect(lakebase_dsn, autocommit=True)
    try:
        await conn.execute(
            f"INSERT INTO {TABLE} "
            f"(run_id, job_run_id, model, status, detail, started_ts, updated_ts, requested_by) "
            f"VALUES ('r1', NULL, '', 'QUEUED', NULL, 500, 500, 'kp')"
        )
    finally:
        await conn.close()

    await report_sql(
        lakebase_dsn, **base_params(status="RUNNING", seq=0, ts=600, updated_ts=600, started_ts=500)
    )

    assert (await current_row(lakebase_dsn, "r1"))["requested_by"] == "kp"


async def test_coalesce_nullif_fills_in_a_model_the_app_left_empty(lakebase_dsn):
    """The app inserts `model = ''` when it creates the row for a run it
    hears about before the job reports, and the column is NOT NULL — so a
    bare COALESCE would see an empty string, keep it, and the run would carry
    no model name for the rest of its life."""
    import psycopg

    conn = await psycopg.AsyncConnection.connect(lakebase_dsn, autocommit=True)
    try:
        await conn.execute(
            f"INSERT INTO {TABLE} "
            f"(run_id, job_run_id, model, status, detail, started_ts, updated_ts) "
            f"VALUES ('r1', NULL, '', 'QUEUED', NULL, 500, 500)"
        )
    finally:
        await conn.close()

    params = base_params(
        status="RUNNING", model="scenario", seq=0, ts=600, updated_ts=600, started_ts=500
    )
    await report_sql(lakebase_dsn, **params)

    assert (await current_row(lakebase_dsn, "r1"))["model"] == "scenario"


# --- cheap structural checks: no database needed ----------------------------


def test_every_named_placeholder_is_a_key_record_summary_produces():
    """A typo in a placeholder name only fails at execution time against a
    real database. This catches it at collection time instead."""
    names = set(re.findall(r"%\((\w+)\)s", REPORT_SQL))
    produced = set(RunRecord("r", model="m").summary(requested_by="x"))
    assert names <= produced, names - produced


def test_run_id_status_and_detail_are_each_reused_between_the_two_inserts():
    """The whole reason for named over positional placeholders: the upsert
    and the history insert must bind these three from the SAME named
    parameter, so the two rows can never end up describing different
    transitions. Positional `$1..$9` needed a comment to say so; a named
    placeholder says it by using the same name twice."""
    for name in ("run_id", "status", "detail"):
        assert REPORT_SQL.count(f"%({name})s") == 2, name


# --- LakebaseStatus, wiring RunRecord.summary() through ---------------------


async def test_a_reported_transition_lands_in_both_tables(lakebase_dsn):
    reporter = LakebaseStatus(lakebase_dsn, schema=SCHEMA)

    assert await reporter.report(record_at("RUNNING", "run started")) is True

    assert reporter.writes == 1 and reporter.failures == 0
    row = await current_row(lakebase_dsn, "run-1")
    assert row["status"] == "RUNNING" and row["detail"] == "run started"
    assert row["job_run_id"] == "jr-7" and row["model"] == "scenario"
    assert len(await history_rows(lakebase_dsn, "run-1")) == 1


async def test_a_report_before_any_status_message_binds_a_null_seq(lakebase_dsn):
    """A job that died before emitting anything still reports, and NULL seq
    is what keeps that row outside the history table's partial unique
    index — there is no message identity to dedupe it by."""
    reporter = LakebaseStatus(lakebase_dsn, schema=SCHEMA)

    assert await reporter.report(RunRecord("run-1", model="scenario")) is True

    row = await current_row(lakebase_dsn, "run-1")
    assert row["status"] == "FAILED", "nothing arrived is not a success"
    rows = await history_rows(lakebase_dsn, "run-1")
    assert len(rows) == 1 and rows[0]["seq"] is None


async def test_both_tables_are_qualified_with_the_configured_schema(postgres):
    """Every statement qualifies its table rather than trusting a
    search_path: a session that had reverted to `public` would find a
    different, empty table instead of failing."""
    import psycopg

    other = "other_schema"
    conn = await psycopg.AsyncConnection.connect(postgres, autocommit=True)
    try:
        await conn.execute(schema_sql(other) + history_schema_sql(other))
        await conn.execute(f"TRUNCATE {other}.run_status, {other}.run_status_history")
    finally:
        await conn.close()

    reporter = LakebaseStatus(postgres, schema=other)
    assert await reporter.report(record_at("RUNNING")) is True

    conn = await psycopg.AsyncConnection.connect(postgres, autocommit=True)
    try:
        cur = await conn.execute(f"SELECT status FROM {other}.run_status WHERE run_id = 'run-1'")
        row = await cur.fetchone()
    finally:
        await conn.close()
    assert row is not None and row[0] == "RUNNING"


async def test_role_overrides_who_the_connection_authenticates_as(lakebase_dsn):
    """`role` is passed as the driver's `user=` override — proven here by making
    it wrong: a role that does not exist must fail authentication rather than
    silently falling back to whatever the DSN's own authority carries."""
    reporter = LakebaseStatus(lakebase_dsn, schema=SCHEMA, role="a-role-that-does-not-exist")

    assert await reporter.report(record_at("RUNNING")) is False

    assert reporter.failures == 1
    assert "does not exist" in (reporter.last_error or "").lower()


# --- degrade paths: never raises, nothing here is load-bearing -------------


async def test_an_unconfigured_reporter_says_nothing_to_nobody():
    """No `DBX_LAKEBASE_DSN` is a normal deploy, not a broken one."""
    attempted = []

    async def connect(password):
        attempted.append(password)
        raise AssertionError("must not be called")

    reporter = LakebaseStatus("", connect=connect)

    assert reporter.available is False
    assert await reporter.report(record_at("RUNNING")) is False
    assert attempted == [] and reporter.failures == 0


async def test_a_connection_failure_is_counted_and_not_raised():
    """Unreachable Lakebase is a live-path problem. `run_events` and the
    end-of-run Delta write still carry the outcome."""

    async def connect(password):
        raise ConnectionError("no route to host")

    reporter = LakebaseStatus("postgresql://unreachable/db", connect=connect)

    assert await reporter.report(record_at("FAILED")) is False

    assert reporter.failures == 1
    assert "ConnectionError" in (reporter.last_error or "")


async def test_a_credential_with_no_token_skips_without_connecting():
    """`AppCredential.token()` returning None means this job has no
    Databricks identity to offer. Lakebase's instance runs
    `enable_pg_native_login: false`, so a token IS the password and there is
    no connection to attempt without one — this is that degrade, kept
    meaningful rather than papered over with a doomed connection attempt.
    """
    attempted = []

    async def connect(password):
        attempted.append(password)
        raise AssertionError("must not attempt a connection with no token")

    reporter = LakebaseStatus(
        "postgresql://lakebase/db",
        credential=AppCredential(env={}),  # no auth-related env vars at all
        connect=connect,
    )

    assert await reporter.report(record_at("RUNNING")) is False

    assert attempted == []
    assert reporter.failures == 1
    assert "credential" in (reporter.last_error or "").lower()


async def test_the_credentials_token_is_used_as_the_connection_password():
    """The Databricks OAuth token IS the password Lakebase accepts —
    `enable_pg_native_login: false` means there is no other kind."""
    seen: list[str | None] = []

    class FakeConn:
        async def execute(self, *_a, **_k):
            return None

        async def close(self):
            pass

    async def connect(password):
        seen.append(password)
        return FakeConn()

    reporter = LakebaseStatus(
        "postgresql://lakebase/db",
        credential=AppCredential(env={"DBX_APP_OAUTH_TOKEN": "oauth-token"}),
        connect=connect,
    )

    assert await reporter.report(record_at("RUNNING")) is True
    assert seen == ["oauth-token"]


async def test_the_password_is_resolved_on_every_connection_not_once():
    """Mirrors `PostgresRunStore`'s own equivalent test
    (`tests/app/test_run_store.py`): a connection is opened and closed per
    report rather than pooled, so a token that rotates mid-run is picked up
    on the very next report rather than staying stale for the rest of it."""
    tokens = iter(["tok-1", "tok-2"])
    seen: list[str | None] = []

    class FakeConn:
        async def execute(self, *_a, **_k):
            return None

        async def close(self):
            pass

    async def connect(password):
        seen.append(password)
        return FakeConn()

    class RotatingCredential:
        async def token(self):
            return next(tokens)

    reporter = LakebaseStatus(
        "postgresql://lakebase/db", credential=RotatingCredential(), connect=connect
    )

    await reporter.report(record_at("RUNNING"))
    await reporter.report(record_at("SUCCEEDED"))

    assert seen == ["tok-1", "tok-2"]


async def test_close_is_a_harmless_no_op():
    """No persistent connection to release — every report opens and closes
    its own (`_conn`), so there is nothing for `close()` to do."""
    reporter = LakebaseStatus("postgresql://lakebase/db")

    await reporter.close()  # must not raise


# --- the DSN split, including the shape only Databricks ever sends ---------


def test_a_lakebase_dsn_becomes_a_tcp_connection_with_tls():
    """The production shape, which no other test here reaches.

    Every DSN in this file comes from `pgserver`, which is a unix socket. The
    one that runs on Databricks is a TLS TCP connection to a real hostname,
    and `connect_kwargs` is the only code that ever looks at it — a misparse
    would surface as an authentication failure against a workspace, which is
    the most expensive place in this project to debug.
    """
    params = connect_kwargs(
        "postgresql://instance-owner@ep-cool-fog-d8vyaasx.database.us-east-2."
        "cloud.databricks.com:5432/databricks_postgres?sslmode=require"
    )

    assert params["host"] == "ep-cool-fog-d8vyaasx.database.us-east-2.cloud.databricks.com"
    assert params["port"] == 5432
    assert params["database"] == "databricks_postgres"
    assert params["user"] == "instance-owner"
    assert "unix_sock" not in params
    assert isinstance(params["ssl_context"], ssl.SSLContext)


def test_sslmode_require_encrypts_without_verifying_like_libpq_does():
    """`require` is not `verify-full`, and this side must not quietly upgrade.

    The DSN is written once in `app/server/config.py` and read by both the app
    (psycopg, which is libpq) and this module. Making one side stricter than
    the other is a difference that shows up only in production.
    """
    context = connect_kwargs("postgresql://u@h:5432/d?sslmode=require")["ssl_context"]
    assert context.check_hostname is False
    assert context.verify_mode is ssl.CERT_NONE

    verifying = connect_kwargs("postgresql://u@h:5432/d?sslmode=verify-full")["ssl_context"]
    assert verifying.check_hostname is True
    assert verifying.verify_mode is ssl.CERT_REQUIRED


def test_sslmode_disable_asks_for_no_tls_at_all():
    assert "ssl_context" not in connect_kwargs("postgresql://u@h:5432/d?sslmode=disable")


def test_a_unix_socket_dsn_names_the_socket_file_not_its_directory():
    """libpq's `host=/dir` means a DIRECTORY; pg8000 wants the socket FILE.

    This is how the dev stack and every other test in this file connect, so a
    regression here would fail loudly — but only after the whole module's
    fixtures had already failed, which reads as "Postgres is broken" rather
    than "the DSN was misparsed".
    """
    params = connect_kwargs("postgresql://postgres:@/postgres?host=/tmp/pgdata")

    assert params["unix_sock"] == "/tmp/pgdata/.s.PGSQL.5432"
    assert params["user"] == "postgres"
    assert params["database"] == "postgres"
    assert "host" not in params and "ssl_context" not in params


def test_a_dsn_with_no_user_falls_back_to_the_os_account_like_libpq():
    """pg8000 requires a user; libpq does not. A DSN libpq would have accepted
    must not start failing because the driver underneath changed."""
    assert connect_kwargs("postgresql://localhost:5432/db")["user"] == getpass.getuser()
