"""The job's Lakebase status writer, against a real Postgres.

Lakebase is standard Postgres; only how the password is obtained differs, and
that is exercised here through a fake token provider (and, for `from_config`,
through the real `M2MTokenProvider` with its HTTP exchange stubbed). The DDL
applied is the committed `lakebase_ddl/*.sql`, as in tests/app/test_run_store.py.

Tests that need no database run everywhere; the rest skip where `pgserver`
has no wheel (aarch64 Linux — see pyproject.toml's dev group).
"""

from __future__ import annotations

import pathlib
import tempfile
from typing import Any

import pytest

from job import auth
from job import lakebase as lb
from job.config import JobConfig
from job.lakebase import LakebaseStatusWriter, from_config
from shared import run_state
from shared.run_state import DEFAULT_SCHEMA, UnsafeSchemaName

DDL_DIR = pathlib.Path(__file__).resolve().parents[2] / "lakebase_ddl"
DDL_FILES = sorted(DDL_DIR.glob("*.sql"))


# --- fixtures -----------------------------------------------------------------


@pytest.fixture(scope="module")
def postgres():
    pgserver = pytest.importorskip("pgserver", reason="needs the dev group")
    directory = pathlib.Path(tempfile.mkdtemp()) / "pg"
    server = pgserver.get_server(directory)
    try:
        yield server.get_uri()
    finally:
        server.cleanup()


def sql(uri: str, text: str, params: Any = None) -> list[tuple] | None:
    import psycopg

    with psycopg.connect(uri, autocommit=True) as conn:
        cur = conn.execute(text, params)
        return cur.fetchall() if cur.description else None


def apply_ddl(uri: str, schema: str = DEFAULT_SCHEMA) -> None:
    """The committed files, with the schema name substituted for a non-default
    one — what their headers tell an operator to do."""
    for path in DDL_FILES:
        text = path.read_text()
        if schema != DEFAULT_SCHEMA:
            text = text.replace(f"{DEFAULT_SCHEMA}.", f"{schema}.").replace(
                f"EXISTS {DEFAULT_SCHEMA};", f"EXISTS {schema};"
            )
        sql(uri, text)


@pytest.fixture
def db(postgres):
    apply_ddl(postgres)
    sql(postgres, "TRUNCATE dbx_leaning.run_status, dbx_leaning.run_status_history")
    return postgres


class Tokens:
    """A token provider that records every fetch and every invalidation."""

    def __init__(self) -> None:
        self.fetches = 0
        self.invalidations = 0

    def token(self) -> str:
        self.fetches += 1
        return f"tok-{self.fetches}"

    def invalidate(self) -> None:
        self.invalidations += 1


def location(uri: str) -> dict[str, Any]:
    from psycopg.conninfo import conninfo_to_dict

    d = conninfo_to_dict(uri)
    return {
        "host": d["host"],
        "port": int(d.get("port") or 5432),
        "database": d.get("dbname") or "postgres",
        "user": d.get("user") or "postgres",
    }


def writer(uri: str, tokens: Tokens | None = None, **kw: Any) -> LakebaseStatusWriter:
    loc = location(uri)
    return LakebaseStatusWriter(
        loc["host"],
        port=loc["port"],
        database=loc["database"],
        user=loc["user"],
        token_provider=tokens or Tokens(),
        sslmode="disable",
        **kw,
    )


def current(uri: str, run_id: str, schema: str = DEFAULT_SCHEMA) -> dict[str, Any] | None:
    rows = sql(
        uri,
        f"SELECT run_id, job_run_id, model, status, terminal, detail, seq, updated_at "
        f"FROM {schema}.run_status WHERE run_id = %s",
        (run_id,),
    )
    if not rows:
        return None
    keys = ("run_id", "job_run_id", "model", "status", "terminal", "detail", "seq", "updated_at")
    return dict(zip(keys, rows[0], strict=True))


def history(uri: str, run_id: str, schema: str = DEFAULT_SCHEMA) -> list[tuple]:
    return (
        sql(
            uri,
            f"SELECT run_id, seq, status, terminal, detail, ts FROM {schema}.run_status_history "
            "WHERE run_id = %s ORDER BY seq, id",
            (run_id,),
        )
        or []
    )


# --- one statement, one text ------------------------------------------------


def test_the_job_issues_the_apps_statement_not_a_copy():
    """Same object, not merely equal text: `store.REPORT_SQL` IS the shared one."""
    from server import store

    assert store.REPORT_SQL is run_state.REPORT_SQL
    assert store.report_sql is run_state.report_sql
    assert (
        store.PostgresRunStore("postgresql://x")._report_sql
        == LakebaseStatusWriter("h", user="u", token_provider=Tokens())._sql
    )


def test_an_unsafe_schema_is_refused_at_construction():
    with pytest.raises(UnsafeSchemaName):
        LakebaseStatusWriter("h", user="u", token_provider=Tokens(), schema="x; DROP TABLE y")


# --- writes against a real Postgres ------------------------------------------


def test_a_write_lands_in_both_tables(db):
    w = writer(db, model="heartbeat", job_run_id="777")
    try:
        assert w.write("r1", 1, "RUNNING", False, None, 1_001) is True
    finally:
        w.close()

    assert current(db, "r1") == {
        "run_id": "r1",
        "job_run_id": "777",
        "model": "heartbeat",
        "status": "RUNNING",
        "terminal": False,
        "detail": None,
        "seq": 1,
        "updated_at": 1_001,
    }
    assert history(db, "r1") == [("r1", 1, "RUNNING", False, None, 1_001)]
    assert (w.writes, w.failures, w.last_error) == (1, 0, None)


def test_one_connection_serves_the_whole_run(db):
    tokens = Tokens()
    w = writer(db, tokens)
    try:
        for seq in range(1, 6):
            assert w.write("r1", seq, "RUNNING", False, None, 1_000 + seq)
    finally:
        w.close()
    assert w.connects == 1
    assert tokens.fetches == 1
    assert w.writes == 5


def test_the_seq_guard_keeps_a_late_write_from_moving_the_row_backwards(db):
    w = writer(db)
    try:
        assert w.write("r1", 5, "SUCCEEDED", True, "done", 1_005)
        # Arrives late, carries an older seq: history takes it, current does not.
        assert w.write("r1", 3, "RUNNING", False, None, 1_003)
        # An exact redelivery is idempotent: one history row, same current row.
        assert w.write("r1", 5, "SUCCEEDED", True, "done", 1_005)
    finally:
        w.close()

    row = current(db, "r1")
    assert (row["status"], row["terminal"], row["seq"], row["detail"]) == (
        "SUCCEEDED",
        True,
        5,
        "done",
    )
    assert [(h[1], h[2]) for h in history(db, "r1")] == [(3, "RUNNING"), (5, "SUCCEEDED")]


def test_seq_compares_as_a_number_not_a_string(db):
    w = writer(db)
    try:
        w.write("r1", 12, "LATER", False, None, 12)
        w.write("r1", 2, "EARLIER", False, None, 2)
    finally:
        w.close()
    assert current(db, "r1")["status"] == "LATER"


def test_model_and_job_run_id_are_carried_on_every_write(db):
    w = writer(db, model="annealing", job_run_id="42")
    try:
        w.write("r1", 1, "RUNNING", False, None, 1)
        w.write("r1", 2, "SUCCEEDED", True, None, 2)
    finally:
        w.close()
    row = current(db, "r1")
    assert (row["model"], row["job_run_id"]) == ("annealing", "42")


def test_a_per_call_value_overrides_and_a_blank_never_erases(db):
    w = writer(db, model="annealing", job_run_id="42")
    try:
        w.write("r1", 1, "RUNNING", False, None, 1, model="other", job_run_id="43")
        assert (current(db, "r1")["model"], current(db, "r1")["job_run_id"]) == ("other", "43")
        # '' is "I do not know", not "erase it" — the NULLIF rule in REPORT_SQL.
        w.write("r1", 2, "RUNNING", False, None, 2, model="", job_run_id="")
    finally:
        w.close()
    row = current(db, "r1")
    assert (row["model"], row["job_run_id"]) == ("other", "43")


def test_a_writer_that_knows_neither_writes_blank_model_and_null_job_run_id(db):
    w = writer(db)
    try:
        w.write("r1", 1, "RUNNING", False, None, 1)
    finally:
        w.close()
    row = current(db, "r1")
    assert (row["model"], row["job_run_id"]) == ("", None)


def test_the_session_carries_a_statement_timeout(db):
    w = writer(db, statement_timeout_s=2.5)
    try:
        w.write("r1", 1, "RUNNING", False, None, 1)
        (value,) = w._conn.execute("SHOW statement_timeout").fetchone()
    finally:
        w.close()
    assert value == "2500ms"


# --- failure is counted, never raised ---------------------------------------


def test_an_unreachable_database_is_counted_not_raised(tmp_path):
    """A socket directory with no server in it: libpq fails at once."""
    w = LakebaseStatusWriter(
        str(tmp_path),
        user="nobody",
        token_provider=Tokens(),
        sslmode="disable",
        connect_timeout_s=1,
    )
    assert w.write("r1", 1, "RUNNING", False, None, 1) is False
    assert w.write("r1", 2, "FAILED", True, None, 2) is False
    assert (w.writes, w.failures) == (0, 2)
    assert w.last_error and w.last_error.startswith("OperationalError")
    w.close()


def test_a_connect_that_raises_is_counted_and_not_retried_on_a_fresh_connection():
    calls: list[dict[str, Any]] = []

    def refuse(**kwargs):
        calls.append(kwargs)
        raise ConnectionRefusedError("nobody home")

    w = LakebaseStatusWriter("h", user="u", token_provider=Tokens(), connect=refuse)
    assert w.write("r1", 1, "RUNNING", False, None, 1) is False
    # One attempt: reconnecting straight after a fresh connect failed would
    # only double the time a dead database costs the harness.
    assert len(calls) == 1
    assert w.failures == 1
    assert w.last_error == "ConnectionRefusedError: nobody home"


def test_a_token_that_cannot_be_fetched_is_counted_not_raised():
    class NoToken:
        def token(self) -> str:
            raise auth.TokenUnavailable("invalid_client")

    w = LakebaseStatusWriter("h", user="u", token_provider=NoToken(), connect=lambda **k: None)
    assert w.write("r1", 1, "RUNNING", False, None, 1) is False
    assert w.last_error == "TokenUnavailable: invalid_client"


def test_bad_arguments_are_counted_not_raised():
    w = LakebaseStatusWriter("h", user="u", token_provider=Tokens(), connect=lambda **k: None)
    assert w.write("r1", "not-a-number", "RUNNING", False, None, 1) is False  # type: ignore[arg-type]
    assert w.failures == 1


def test_the_connect_is_bounded_and_carries_the_token_as_password():
    seen: dict[str, Any] = {}

    class Conn:
        def execute(self, *a, **k):
            return None

        def close(self):
            return None

    def connect(**kwargs):
        seen.update(kwargs)
        return Conn()

    w = LakebaseStatusWriter(
        "instance.database.cloud.databricks.com",
        user="client-id-123",
        token_provider=Tokens(),
        connect=connect,
        connect_timeout_s=3,
    )
    assert w.write("r1", 1, "RUNNING", False, None, 1)
    assert seen["host"] == "instance.database.cloud.databricks.com"
    assert seen["user"] == "client-id-123"
    assert seen["password"] == "tok-1"
    assert seen["dbname"] == "databricks_postgres"
    assert seen["sslmode"] == "require"
    assert seen["connect_timeout"] == 3
    assert seen["autocommit"] is True


def test_a_dropped_connection_reconnects_once_with_a_fresh_token(db):
    tokens = Tokens()
    w = writer(db, tokens)
    try:
        assert w.write("r1", 1, "RUNNING", False, None, 1)
        pid = w._conn.info.backend_pid
        # The database drops us — a restart, a failover, an idle reaper.
        sql(db, "SELECT pg_terminate_backend(%s)", (pid,))

        assert w.write("r1", 2, "SUCCEEDED", True, None, 2) is True
    finally:
        w.close()

    assert (w.writes, w.failures) == (2, 0)
    assert w.connects == 2
    assert tokens.invalidations == 1
    assert tokens.fetches == 2  # the reconnect did not reuse the cached token
    assert current(db, "r1")["status"] == "SUCCEEDED"


def test_a_reconnect_that_also_fails_is_one_failure():
    class Dies:
        def __init__(self) -> None:
            self.n = 0

        def execute(self, text, params=None):
            self.n += 1
            if "set_config" not in text and self.n > 2:
                raise OSError("connection reset")

        def close(self):
            return None

    attempts = {"n": 0}

    def connect(**kwargs):
        attempts["n"] += 1
        if attempts["n"] > 1:
            raise ConnectionRefusedError("still down")
        return Dies()

    tokens = Tokens()
    w = LakebaseStatusWriter("h", user="u", token_provider=tokens, connect=connect)
    assert w.write("r1", 1, "RUNNING", False, None, 1) is True
    assert w.write("r1", 2, "RUNNING", False, None, 2) is False
    assert attempts["n"] == 2
    assert (w.writes, w.failures) == (1, 1)
    assert w.last_error == "ConnectionRefusedError: still down"


def test_close_is_idempotent_and_a_write_after_it_is_counted(db):
    w = writer(db)
    w.write("r1", 1, "RUNNING", False, None, 1)
    w.close()
    w.close()
    assert w.write("r1", 2, "RUNNING", False, None, 2) is False
    assert w.last_error == "RuntimeError: writer is closed"


# --- the app's store and the job's writer agree ------------------------------


REPORTS = [
    # (run_id, seq, status, terminal, detail, ts, model, job_run_id)
    ("a", 1, "RUNNING", False, None, 1_001, "heartbeat", "9"),
    ("a", 4, "INFEASIBLE", True, "no feasible schedule", 1_004, "", None),
    ("a", 2, "RUNNING", False, "late", 1_002, "heartbeat", "9"),
    ("a", 4, "INFEASIBLE", True, "no feasible schedule", 1_004, "", None),
    ("b", 1, "RUNNING", False, None, 2_001, "", None),
    ("b", 12, "CANCELLED", True, None, 2_012, "annealing", "10"),
]


async def test_the_apps_store_and_the_jobs_writer_produce_identical_rows(postgres):
    from server.store import PostgresRunStore

    for schema in ("app_side", "job_side"):
        apply_ddl(postgres, schema)
        sql(postgres, f"TRUNCATE {schema}.run_status, {schema}.run_status_history")

    store = PostgresRunStore(postgres, schema="app_side")
    w = writer(postgres, schema="job_side")
    try:
        for run_id, seq, status, terminal, detail, ts, model, job_run_id in REPORTS:
            await store.set_status(
                run_id,
                status,
                seq=seq,
                terminal=terminal,
                ts=ts,
                detail=detail,
                model=model,
                job_run_id=job_run_id,
            )
            assert w.write(
                run_id, seq, status, terminal, detail, ts, model=model, job_run_id=job_run_id
            )
    finally:
        w.close()

    for run_id in ("a", "b"):
        assert current(postgres, run_id, "app_side") == current(postgres, run_id, "job_side")
        assert history(postgres, run_id, "app_side") == history(postgres, run_id, "job_side")
    # And the rows are the interesting ones, not two equal empties.
    assert current(postgres, "a", "job_side")["status"] == "INFEASIBLE"
    assert current(postgres, "b", "job_side")["model"] == "annealing"
    assert len(history(postgres, "a", "job_side")) == 3


# --- from_config ---------------------------------------------------------------


CONFIGURED = {
    "run_id": "r1",
    "model_spec": "heartbeat",
    "job_run_id": "555",
    "workspace_host": "https://example.cloud.databricks.com",
    "lakebase_host": "instance.database.cloud.databricks.com",
    "lakebase_secret_scope": "dbx",
    "lakebase_client_id_key": "lakebase-id",
    "lakebase_secret_key": "lakebase-secret",
}


def record_secret_reads(monkeypatch, values: dict[tuple[str, str], str] | None = None):
    reads: list[tuple[str, str]] = []

    def fake(scope: str, key: str) -> str | None:
        reads.append((scope, key))
        return (values or {}).get((scope, key))

    monkeypatch.setattr("job.auth.read_secret", fake)
    return reads


@pytest.mark.parametrize(
    "missing",
    [
        "lakebase_host",
        "lakebase_secret_scope",
        "lakebase_client_id_key",
        "lakebase_secret_key",
        "workspace_host",
    ],
)
def test_from_config_is_none_when_unconfigured_and_reads_no_secret(monkeypatch, caplog, missing):
    reads = record_secret_reads(monkeypatch)
    cfg = JobConfig(**{**CONFIGURED, missing: None})

    with caplog.at_level("INFO", logger="job.lakebase"):
        assert from_config(cfg) is None

    assert reads == []
    lines = [r.getMessage() for r in caplog.records if r.name == "job.lakebase"]
    assert len(lines) == 1
    env_name = {
        "lakebase_host": "DBX_LAKEBASE_HOST",
        "lakebase_secret_scope": "DBX_LAKEBASE_OAUTH_SECRET_SCOPE",
        "lakebase_client_id_key": "DBX_LAKEBASE_OAUTH_CLIENT_ID_KEY",
        "lakebase_secret_key": "DBX_LAKEBASE_OAUTH_SECRET_KEY",
        "workspace_host": "DATABRICKS_HOST",
    }[missing]
    assert env_name in lines[0]


def test_from_config_with_nothing_at_all_is_none(monkeypatch):
    reads = record_secret_reads(monkeypatch)
    assert from_config(JobConfig(run_id="r1", model_spec="heartbeat")) is None
    assert reads == []


def test_from_config_is_none_when_the_secrets_cannot_be_read(monkeypatch, caplog):
    record_secret_reads(monkeypatch)  # every read returns None
    with caplog.at_level("INFO", logger="job.lakebase"):
        assert from_config(JobConfig(**CONFIGURED)) is None
    assert any("secret scope dbx" in r.getMessage() for r in caplog.records)


def test_from_config_is_none_for_an_unsafe_schema(monkeypatch):
    record_secret_reads(
        monkeypatch,
        {("dbx", "lakebase-id"): "client-abc", ("dbx", "lakebase-secret"): "shh"},
    )
    assert from_config(JobConfig(**{**CONFIGURED, "lakebase_schema": "a;b"})) is None


def test_from_config_builds_a_writer_from_one_read_of_the_scope(monkeypatch):
    reads = record_secret_reads(
        monkeypatch,
        {("dbx", "lakebase-id"): "client-abc", ("dbx", "lakebase-secret"): "shh"},
    )
    w = from_config(JobConfig(**{**CONFIGURED, "lakebase_port": 6543, "lakebase_schema": "s2"}))

    assert isinstance(w, LakebaseStatusWriter)
    assert reads == [("dbx", "lakebase-id"), ("dbx", "lakebase-secret")]
    assert w.user == "client-abc"  # the Postgres role IS the principal's client id
    assert (w.host, w.port, w.database, w.schema) == (
        "instance.database.cloud.databricks.com",
        6543,
        "databricks_postgres",
        "s2",
    )
    assert (w.model, w.job_run_id) == ("heartbeat", "555")
    assert isinstance(w._tokens, auth.M2MTokenProvider)
    assert w._tokens.url == "https://example.cloud.databricks.com/oidc/v1/token"


def test_from_config_takes_explicit_model_and_job_run_id(monkeypatch):
    record_secret_reads(
        monkeypatch,
        {("dbx", "lakebase-id"): "client-abc", ("dbx", "lakebase-secret"): "shh"},
    )
    w = from_config(JobConfig(**CONFIGURED), model="annealing", job_run_id="999")
    assert w is not None
    assert (w.model, w.job_run_id) == ("annealing", "999")


def test_from_config_end_to_end_reads_secrets_once_for_many_writes(db, monkeypatch):
    """The real M2M provider, its HTTP exchange stubbed; the real database,
    reached by swapping the Lakebase address for the local one at connect."""
    reads = record_secret_reads(
        monkeypatch,
        {("dbx", "lakebase-id"): "client-abc", ("dbx", "lakebase-secret"): "shh"},
    )
    fetches: list[int] = []

    def fetch(self):
        fetches.append(1)
        return f"oauth-{len(fetches)}", 3600.0

    monkeypatch.setattr(auth.M2MTokenProvider, "_fetch", fetch)

    import psycopg

    loc = location(db)
    seen: list[dict[str, Any]] = []

    def connect(**kwargs):
        seen.append(dict(kwargs))
        kwargs.update(host=loc["host"], port=loc["port"], dbname=loc["database"])
        kwargs.update(user=loc["user"], sslmode="disable")
        return psycopg.connect(**kwargs)

    w = from_config(JobConfig(**CONFIGURED), connect=connect)
    assert w is not None
    try:
        for seq in (1, 2, 3):
            assert w.write("r1", seq, "RUNNING" if seq < 3 else "SUCCEEDED", seq == 3, None, seq)
    finally:
        w.close()

    assert len(reads) == 2  # once per key, for the whole run
    assert len(fetches) == 1
    assert [(s["user"], s["password"]) for s in seen] == [("client-abc", "oauth-1")]
    row = current(db, "r1")
    assert (row["model"], row["job_run_id"], row["status"]) == ("heartbeat", "555", "SUCCEEDED")


def test_the_module_exports_what_the_harness_will_plug_in():
    assert set(lb.__all__) >= {"LakebaseStatusWriter", "from_config"}
