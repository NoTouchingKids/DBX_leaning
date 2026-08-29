"""Reporting the run's status to Lakebase, over a direct Postgres connection.

**Why the job writes this at all.** `run_status` is the record of truth for
"what is this run doing", and until now only the app maintained it — from
status messages arriving over the socket. That made a fact about the run
depend on the observer being up and the socket being healthy. It is not:
the job knows its own status, so the job reports it.

The consequence worth having: a run whose socket never attaches — the app was
down, the proxy refused the handshake, the deploy has no `DBX_APP_URL` — still
keeps `run_status` current, instead of sitting at whatever the app last saw
until startup reconciliation notices.

**Who writes what.** The app still owns the slot claim: the count-and-claim
transaction that makes the 5-concurrent-task ceiling real, and the row's
creation at trigger time. The job owns the status transitions on that row.
One writer per concern, and the UPSERT's `updated_ts` guard means an
out-of-order write cannot move the row backwards even so.

**A direct Postgres connection, not the Database REST API.** The original
design here posted `{"statement": ..., "parameters": [...]}` to a Database
REST API endpoint, on the reasoning that a Postgres driver in this process
would be paid for by all ten model environments (one job per model, each with
its own dependency list — `CLAUDE.md`) while `httpx` was already here for the
OAuth exchange in `job/auth.py`. That reasoning was sound and the conclusion
was wrong: the URL Lakebase actually hands out is a PostgREST base
(`/api/2.0/workspace/<id>/rest/<database>`), which does not accept raw SQL at
all. There was no shape of request that would have made the REST version
work — this was not a tuning problem, it was the wrong protocol. So a driver
it is, and `app/server/store.py::PostgresRunStore` had the rest of the
answer: connect directly, resolve the password fresh per connection, because
Lakebase's instance runs `enable_pg_native_login: false` and accepts nothing
else.

**pg8000, not psycopg, and the reason is the runtime — not taste.** The app
uses psycopg and should keep it; this process cannot. `psycopg[binary]` ships
a compiled `psycopg_binary.pq` that dlopens a wheel-bundled libpq and its own
OpenSSL. Loaded into the Databricks serverless Python kernel — which already
has gRPC (databricks-connect), pyarrow and their native TLS in the process —
that load calls `abort()`:

    psycopg/pq/__init__.py:83 in import_from_libpq   ->  from psycopg_binary import pq
    <frozen importlib._bootstrap_external>:1289 in create_module
    Fatal Python error: Aborted

`abort()` is not an exception. psycopg's own fallback chain — C, then binary,
then the ctypes one — is `try/except Exception` around each import, and none
of it gets a turn, because there is no interpreter left to raise in. The task
dies with no traceback beyond that faulthandler dump, and the run reports
nothing. That is the second thing on this runtime to die that way, after the
numpy-ABI mismatch in `scripts/export_requirements.py`, and the pattern is
the same: native code loaded beside the runtime's own native code.

pg8000 has no native code at all — it speaks the Postgres wire protocol in
Python, and its TLS is the standard library's `ssl`. There is nothing to
dlopen, so there is nothing to abort. It is also about 120 KB against
psycopg[binary]'s ~5 MB, in ten environments, which the REST attempt was
trying to save in the first place.

What it costs: pg8000 is synchronous, so `_conn` hands back a small async
facade that hops to a worker thread. That is the whole price, and it is paid
by a path that runs a handful of times per run.

**Two rows per report, one statement.** `run_status` holds current state, one
row per run; `run_status_history` holds every transition that was reported,
append-only. They go out as a single statement so they share a
transaction — history holding a transition the current-state row never got
would be a worse record than either table alone — and so a path that runs on
the way into and out of every run costs one round trip, not two.

**Named placeholders, not positional.** The statement reuses `run_id`,
`status` and `detail` between the upsert and the history insert on purpose,
so the two rows cannot end up describing different transitions — see the
comment on `REPORT_SQL`. Positional `%s` cannot express "the same parameter,
twice"; the `%(name)s` form can, directly off `RunRecord.summary()`'s own
keys, with no positional re-mapping to keep in step with the SQL text.

This is also what keeps ONE dialect across two drivers. `%(name)s` is
psycopg's native style and pg8000's `pyformat`, so `REPORT_SQL` here and the
statements in `app/server/store.py` are the same language against the same
database — the property that mattered when both sides used psycopg, kept now
that they do not.

**Nothing here is load-bearing.** Unconfigured, no credential, unreachable,
refused — all of them log and carry on. The durable record of a status
transition is the `run_events` row the harness writes for every status
message (`shared/tables.py` routes it there); this is the live, point-lookup
copy that the app and a browser read. Losing it costs freshness, not the
record.
"""

from __future__ import annotations

import asyncio
import getpass
import logging
import ssl
import urllib.parse
from typing import Any

from .auth import AppCredential
from .record import RunRecord

log = logging.getLogger(__name__)

__all__ = ["LakebaseStatus", "REPORT_SQL"]

#: Upsert rather than update: a job triggered outside the app (a schedule, a
#: manual `run-now`) has no row yet, and refusing to record its status
#: because nobody claimed a slot for it would lose exactly the runs nobody is
#: watching.
#:
#: The `WHERE` guard is what makes two writers safe. The app writes this row
#: too, and Databricks can deliver a retry out of order; without the guard a
#: late RUNNING would overwrite a SUCCEEDED that already landed.
#:
#: The upsert rides as a data-modifying CTE and the history append is the
#: primary query. Postgres runs a data-modifying CTE exactly once and always
#: to completion, whether or not the primary query reads its output, so
#: `upsert_current` is not dead code despite nothing selecting from it.
#:
#: **The history row appends even when the guard makes the upsert a no-op.**
#: That reads like a bug and is the point: the two tables answer different
#: questions. Current state is what is true, so a stale RUNNING arriving after
#: SUCCEEDED must not move it backwards. History is what was *reported*, and
#: that the stale transition arrived at all is the fact you want when working
#: out why the row looks the way it does.
#:
#: `ON CONFLICT DO NOTHING` names no inference target because the unique index
#: is partial (`UNIQUE (run_id, seq) WHERE seq IS NOT NULL`) and naming it
#: would restate that predicate in a second place, free to drift from the DDL.
#: It is what makes a redelivered report idempotent: one status message, one
#: history row, however many times Databricks retries the task. A report made
#: before any status message exists binds a NULL `seq`, falls outside the
#: index and always appends — there is no message identity to dedupe it by.
#:
#: `NULLIF` on `model`, not a bare `COALESCE`. The column is NOT NULL and the
#: app inserts `''` when it creates the row for a run it hears about before
#: the job reports — so a plain COALESCE sees an empty string rather than a
#: NULL, keeps it, and the run carries `model=''` for the rest of its life.
#:
#: `recorded_by` is stated rather than left to the column default, so these
#: rows stay labelled as the job's if the app ever writes this table too.
#:
#: **Named placeholders, bound from a dict** (`RunRecord.summary()`, plus
#: `requested_by` which nothing here binds — see its own docstring). The
#: driver pulls only the names this statement actually references out of that
#: dict — true of psycopg's `%(name)s` and of pg8000's `pyformat` alike — so
#: the extra key costs nothing and does not have to be filtered out first.
#: `%(run_id)s`, `%(status)s` and `%(detail)s` each appear twice —
#: once in the upsert, once in the history insert — which is what keeps the
#: two rows from ever disagreeing about the transition they describe; a
#: positional `$1..$9` statement had to say so in a comment; a named one says
#: it by using the same name.
REPORT_SQL = """
WITH upsert_current AS (
    INSERT INTO {schema}.run_status
        (run_id, job_run_id, model, status, detail, started_ts, updated_ts)
    VALUES
        (%(run_id)s, %(job_run_id)s, %(model)s, %(status)s, %(detail)s,
         %(started_ts)s, %(updated_ts)s)
    ON CONFLICT (run_id) DO UPDATE SET
        status      = EXCLUDED.status,
        detail      = EXCLUDED.detail,
        updated_ts  = EXCLUDED.updated_ts,
        job_run_id  = COALESCE(EXCLUDED.job_run_id, {schema}.run_status.job_run_id),
        model       = COALESCE(NULLIF({schema}.run_status.model, ''), EXCLUDED.model),
        started_ts  = COALESCE({schema}.run_status.started_ts, EXCLUDED.started_ts)
    WHERE {schema}.run_status.updated_ts <= EXCLUDED.updated_ts
)
INSERT INTO {schema}.run_status_history
    (run_id, seq, status, detail, ts, recorded_by)
VALUES (%(run_id)s, %(seq)s, %(status)s, %(detail)s, %(ts)s, 'job')
ON CONFLICT DO NOTHING
""".strip()


#: libpq spells "connect over a unix socket" as `host=/some/directory`, and
#: names the socket `.s.PGSQL.<port>` inside it. pg8000 wants the socket FILE.
#: Not a theoretical branch: the dev stack and every test here arrive that way
#: (`pgserver` hands out `postgresql://postgres:@/postgres?host=/tmp/.../pg`).
_SOCKET_NAME = ".s.PGSQL.{port}"

#: libpq's `sslmode` values that encrypt, in the order libpq ranks them. Only
#: the `verify-` pair check the certificate; `require` encrypts and trusts.
#: Mirrored rather than improved on: the DSN is written once in
#: `app/server/config.py` and read by both the app (psycopg) and this module,
#: so making one side stricter than the other would be a difference that only
#: shows up in production. Upgrading is a one-word change to that DSN, and
#: both sides pick it up.
_SSL_VERIFYING = frozenset({"verify-ca", "verify-full"})
_SSL_ENCRYPTING = _SSL_VERIFYING | {"allow", "prefer", "require"}


def connect_kwargs(dsn: str) -> dict[str, Any]:
    """A libpq connection URI as pg8000 keyword arguments.

    pg8000 takes ``host``/``port``/``database``/``unix_sock`` rather than a
    connection string, so the URI everything else in this repo passes around —
    ``DBX_LAKEBASE_DSN``, `app/server/config.py`, the dev stack's embedded
    Postgres — is split here, once, instead of at each call site.

    Public because `tests/job/test_lakebase_status.py` asserts on the split
    directly: a DSN is the one input to this module that arrives from
    deployment config rather than from code, and a silent misparse would show
    up as an authentication failure rather than as a parse error.
    """
    parts = urllib.parse.urlsplit(dsn)
    query = {key: values[-1] for key, values in urllib.parse.parse_qs(parts.query).items()}

    params: dict[str, Any] = {
        # libpq defaults the database to the user name; so does the server
        # when the startup packet omits it, which is what None does here.
        "database": urllib.parse.unquote(parts.path).lstrip("/") or None,
        # pg8000 requires a user — libpq falls back to the OS account, so do
        # the same rather than failing on a DSN libpq would have accepted.
        "user": urllib.parse.unquote(parts.username) if parts.username else getpass.getuser(),
    }
    if parts.password:
        params["password"] = urllib.parse.unquote(parts.password)

    port = parts.port or int(query.get("port") or 5432)
    host = query.get("host", "")
    if host.startswith("/"):
        params["unix_sock"] = f"{host.rstrip('/')}/{_SOCKET_NAME.format(port=port)}"
        return params

    params["host"] = parts.hostname or host or "localhost"
    params["port"] = port
    sslmode = (query.get("sslmode") or "prefer").lower()
    if sslmode in _SSL_ENCRYPTING:
        context = ssl.create_default_context()
        if sslmode not in _SSL_VERIFYING:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        params["ssl_context"] = context
    return params


class _AsyncConnection:
    """The two methods `report` uses, over a synchronous pg8000 connection.

    pg8000 has no async API, so each call hops to a worker thread — three per
    report (connect, execute, close). That is the trade for a driver with no
    native code to abort on this runtime, and it is paid by a path that runs
    a handful of times per run.

    The connection is used from whatever thread the executor happens to pick,
    but never from two at once: `report` awaits each call before making the
    next, and opens a fresh connection every time.
    """

    def __init__(self, raw: Any) -> None:
        self._raw = raw

    async def execute(self, sql: str, params: Any = None) -> None:
        await asyncio.to_thread(self._execute, sql, params)

    def _execute(self, sql: str, params: Any) -> None:
        cursor = self._raw.cursor()
        try:
            cursor.execute(sql, params)
        finally:
            cursor.close()

    async def close(self) -> None:
        await asyncio.to_thread(self._raw.close)


class LakebaseStatus:
    """One status row kept current, and one history row per report, over a
    direct Postgres connection.

    ``dsn`` is a connection string with no credential in it — host, port,
    database and ``sslmode=require`` — mirroring
    ``app/server/store.py::PostgresRunStore``: Lakebase's password is a
    short-lived Databricks OAuth token, so baking one in here would be stale
    within the hour. ``role`` is the Postgres role to connect as, kept
    alongside the DSN rather than folded into it for the same reason the app
    keeps ``lakebase_user`` beside ``lakebase_dsn`` — see
    ``DBX_LAKEBASE_DSN`` / ``DBX_LAKEBASE_USER`` in ``job/config.py``.

    A connection is opened and closed per report rather than pooled, the same
    trade `PostgresRunStore` makes and for the same reason: the volume is a
    handful of statements per run, and resolving the credential fresh on each
    connect makes token rotation a non-issue instead of a pool-invalidation
    problem.
    """

    def __init__(
        self,
        dsn: str,
        *,
        schema: str = "dbx_leaning",
        role: str | None = None,
        credential: AppCredential | None = None,
        connect_timeout_s: float = 10.0,
        connect: Any = None,
    ) -> None:
        self.dsn = dsn
        self.schema = schema
        self.role = role
        self.credential = credential
        self.connect_timeout_s = connect_timeout_s
        self._connect = connect  # injectable for tests
        self.writes = 0
        self.failures = 0
        self.last_error: str | None = None

    @property
    def available(self) -> bool:
        return bool(self.dsn)

    async def _conn(self, password: str | None) -> Any:
        """One connection. ``password`` is whatever `report` resolved —
        already-fetched, so this never touches `self.credential` itself and
        stays trivial to fake in tests.
        """
        if self._connect is not None:
            return await self._connect(password)
        return _AsyncConnection(await asyncio.to_thread(self._open, password))

    def _open(self, password: str | None) -> Any:
        """The blocking half of `_conn`, run in a worker thread."""
        import pg8000.dbapi

        # pg8000 reads the paramstyle from its own module global at execute
        # time — there is no per-connection setting — so it is set here on
        # every open rather than once at import. Nothing then depends on
        # which module imported first, and this process has exactly one
        # pg8000 user to disturb.
        #
        # The suppression is a false positive, not a papered-over bug: DB-API
        # 2.0 defines `paramstyle` as a module attribute the caller may set,
        # and pg8000 has no type stubs, so ty reads the module's own
        # `paramstyle = "format"` as `Literal["format"]` and calls any other
        # value an invalid assignment. The tests in
        # `tests/job/test_lakebase_status.py` run `REPORT_SQL` through this
        # against a real PostgreSQL, which is what settles it.
        pg8000.dbapi.paramstyle = "pyformat"  # ty: ignore[invalid-assignment]

        params = connect_kwargs(self.dsn)
        # Seconds, and pg8000 applies it to the SOCKET rather than only to
        # the connect: it calls `settimeout` once and never clears it, so this
        # bounds the whole conversation — dialling an unreachable instance and
        # waiting on a statement alike. Stricter than libpq's `connect_timeout`
        # (connect only), and the stricter reading is the one wanted here: a
        # report is one small write, `report` never raises, and the durable
        # `run_events` row carries the transition either way. Named
        # `connect_timeout_s` still, because that is what callers set it for
        # and what `job/config.py` calls it.
        params["timeout"] = max(1, int(self.connect_timeout_s))
        if self.role:
            # A keyword overrides whatever the DSN says, so the DSN never has
            # to carry a role at all — the same reason `password` below is a
            # keyword rather than baked into `self.dsn`.
            params["user"] = self.role
        if password is not None:
            params["password"] = password
        conn = pg8000.dbapi.connect(**params)
        conn.autocommit = True
        return conn

    async def report(self, record: RunRecord, *, requested_by: str | None = None) -> bool:
        """Report the record's current status — both rows. True when it landed.

        Never raises: a status report that cannot be delivered is a live-path
        problem, and the live path is never load-bearing here.
        """
        if not self.available:
            return False
        summary = record.summary(requested_by=requested_by)

        password: str | None = None
        if self.credential is not None:
            password = await self.credential.token()
            if not password:
                # Lakebase's instance runs `enable_pg_native_login: false`,
                # so a token IS the password and there is no connection to
                # attempt without one. Skip rather than dial Postgres with no
                # password and let THAT fail: the log then names the real
                # reason ("no credential") instead of a generic Postgres
                # authentication error, and a host known in advance not to
                # answer costs no round trip. `AppCredential.token()`
                # returning None here is the same "no Databricks identity to
                # offer" case the WS bus degrades from quietly; this is that
                # same degrade, kept meaningful rather than papered over with
                # a doomed connection attempt.
                self.last_error = "no Databricks credential available for Lakebase"
                self.failures += 1
                log.info(
                    "no Databricks credential for %s -> %s; Lakebase accepts only a "
                    "short-lived OAuth token as its password, so this report is "
                    "skipped rather than attempted. the run_events row on the "
                    "durable path still carries this transition.",
                    summary["run_id"],
                    summary["status"],
                )
                return False

        try:
            conn = await self._conn(password)
            try:
                # `summary` carries `requested_by` too, which nothing in
                # REPORT_SQL references — the driver pulls only the names the
                # statement actually uses, so passing the whole dict is
                # correct, not merely convenient. See the comment on
                # REPORT_SQL.
                await conn.execute(REPORT_SQL.format(schema=self.schema), summary)
            finally:
                await conn.close()
            ok = True
        except Exception as exc:  # noqa: BLE001 - every failure mode is "log and carry on"
            ok = False
            self.last_error = f"{type(exc).__name__}: {exc}"

        if ok:
            self.writes += 1
            return True
        self.failures += 1
        log.info(
            "could not report %s -> %s to Lakebase (%s); the run_events row "
            "on the durable path still carries it",
            summary["run_id"],
            summary["status"],
            self.last_error,
        )
        return False

    async def close(self) -> None:
        """No persistent connection to release — every report opens and
        closes its own (see `_conn`), so this is a no-op. Kept because
        callers (`job/runner.py`) call it unconditionally at teardown and
        should not have to know that this implementation has nothing to
        clean up.
        """
        return None
