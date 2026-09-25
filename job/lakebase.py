"""The job reports its own status to Lakebase: ``run_status`` and its history.

**Why the job writes this at all.** ``run_status`` is the record of truth for
"what is this run doing", and until v5 only the app maintained it — from status
messages arriving over the socket. That made a fact about the run depend on the
observer being up. It is not: the job knows its own status, so the job reports
it (``docs/v5-implementation-plan.md``, Phase 2 items 1 and 4). A run whose
socket never attaches still keeps ``run_status`` current.

**The statement is the app's, not a copy of it.** :data:`shared.run_state.REPORT_SQL`
— the seq-guarded upsert of the current row plus the deduped history append, in
one statement — is the exact text ``app/server/store.py`` issues, and so are
the parameter coercions (:func:`shared.run_state.report_params`). Until Phase 2
item 5 retires the app-side writer, both processes write the same row; the seq
guard is what makes that safe, and sharing the text is what makes the guard the
same guard.

**The transport is a normal Postgres connection**, authenticated with a
short-lived Databricks OAuth token as the password — what ``app/server/store.py``
and ``app/server/oauth.py`` do for the app, and what Phase 0 item 1 confirmed
against a real workspace. Not the Database REST API ``origin/lakebase-status-history``
assumed: that API issues credentials, it does not run statements. The Postgres
USER is the Lakebase principal's own client id.

**Synchronous**, because the job has no event loop (Phase 3). It is called from
the harness's status path on whichever thread the harness gives it — never
``emit()`` itself, since a slow write must not stall the model.

**One connection per run, not per write**, unlike the app. The app runs for up
to 24 hours and opens a connection per operation so the token is always fresh;
a job writes a handful of statements over one run, so it holds one connection
and reconnects — with a token fetched again, not the cached one — when that
connection fails. Postgres checks the password only at connect, so a
connection outliving its token's ~60 minutes keeps working; it is a NEW
connection that needs a new token, which is exactly when one is fetched.

**Bounded, so a hung database cannot hold the harness past its shutdown
budget.** ``connect_timeout`` bounds the connect, ``statement_timeout`` bounds
the server's work, and TCP keepalives plus ``tcp_user_timeout`` bound a peer
that has vanished mid-statement. A write that fails on a reused connection
reconnects once and retries; one that fails on a fresh connection does not,
since reconnecting straight away would only repeat it. Worst case per write,
then, is about two connects plus two statements.

**Never raises; counts instead** (Phase 0 item 4, rule 3). ``writes``,
``failures`` and ``last_error`` make a best-effort path observable without
making it load-bearing. The durable record of every status transition is the
``run_events`` part file the harness writes regardless; this is the live,
point-lookup copy the app reads. Losing it costs freshness, not the record.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any, Protocol

from shared.run_state import DEFAULT_SCHEMA, report_params, report_sql

from . import auth
from .config import JobConfig

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_CONNECT_TIMEOUT_S",
    "DEFAULT_STATEMENT_TIMEOUT_S",
    "LakebaseStatusWriter",
    "TokenProvider",
    "from_config",
]

#: libpq's `connect_timeout`, in whole seconds (libpq treats anything below 2
#: as 2). Per attempt; a write makes at most two.
DEFAULT_CONNECT_TIMEOUT_S = 5.0

#: Server-side `statement_timeout`, and the basis of the TCP-level bounds.
#: The report statement touches two rows by primary/unique key; anything near
#: this long is a database in trouble, not a slow query.
DEFAULT_STATEMENT_TIMEOUT_S = 5.0

APPLICATION_NAME = "dbx_leaning-job"


class TokenProvider(Protocol):
    """Anything that hands out a bearer token. `job.auth.M2MTokenProvider` is
    the real one; it may also have `invalidate()`, which is called before a
    reconnect so the next `token()` is fetched rather than cached."""

    def token(self) -> str: ...


class LakebaseStatusWriter:
    """Reports status transitions to ``run_status`` and ``run_status_history``.

    ``model`` and ``job_run_id`` are the run's, given once here and carried on
    every write — the app-side writer never knew them, which is why ``model``
    used to stay ``''``. A per-call value overrides them.

    ``connect`` is injectable for tests: called with libpq keyword arguments
    (``host``, ``port``, ``dbname``, ``user``, ``password``, ...) plus
    ``autocommit=True``, returning a psycopg-shaped connection. Defaults to
    ``psycopg.connect``.
    """

    def __init__(
        self,
        host: str,
        *,
        user: str,
        token_provider: TokenProvider,
        port: int = 5432,
        database: str = "databricks_postgres",
        schema: str = DEFAULT_SCHEMA,
        model: str | None = None,
        job_run_id: str | None = None,
        sslmode: str = "require",
        connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
        statement_timeout_s: float = DEFAULT_STATEMENT_TIMEOUT_S,
        connect: Callable[..., Any] | None = None,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.database = database
        self.schema = schema
        self.user = user
        self.model = model
        self.job_run_id = job_run_id
        self.sslmode = sslmode
        self.connect_timeout_s = float(connect_timeout_s)
        self.statement_timeout_s = float(statement_timeout_s)
        #: Vetted here: a bad schema name is a configuration error, and it
        #: raises at construction rather than failing every write quietly.
        self._sql = report_sql(schema)
        self._tokens = token_provider
        self._connect = connect
        self._conn: Any = None
        self._closed = False
        #: The harness may write from one thread and close from another at
        #: shutdown; one connection is not safe to share between them.
        self._lock = threading.Lock()

        self.writes = 0
        self.failures = 0
        self.connects = 0
        self.last_error: str | None = None

    # -- the one public operation --------------------------------------------

    def write(
        self,
        run_id: str,
        seq: int,
        status: str,
        terminal: bool,
        detail: str | None,
        ts: int,
        *,
        model: str | None = None,
        job_run_id: str | None = None,
    ) -> bool:
        """Report one status message. True when it landed; never raises.

        ``seq``, ``terminal`` and ``ts`` are the MESSAGE's, not this process's
        clock — the seq guard compares the message's own ordering, so a late
        write cannot move the row backwards. A report the guard refuses still
        returns True: it landed, in history, which is what it should do.
        """
        try:
            params = report_params(
                run_id,
                status,
                seq=seq,
                terminal=terminal,
                ts=ts,
                detail=detail,
                model=self.model if model is None else model,
                job_run_id=self.job_run_id if job_run_id is None else job_run_id,
            )
        except (TypeError, ValueError) as exc:
            return self._failed(run_id, status, exc)

        with self._lock:
            if self._closed:
                return self._failed(run_id, status, RuntimeError("writer is closed"))
            reused = self._conn is not None
            try:
                self._execute(params)
            except Exception as exc:  # noqa: BLE001 - every failure is "count and carry on"
                self._drop()
                if not reused:
                    return self._failed(run_id, status, exc)
                log.info(
                    "Lakebase connection failed mid-run (%s); reconnecting once with a fresh token",
                    _describe(exc),
                )
                try:
                    self._execute(params)
                except Exception as retry_exc:  # noqa: BLE001
                    self._drop()
                    return self._failed(run_id, status, retry_exc)
            self.writes += 1
            return True

    def close(self) -> None:
        """Close the connection. Idempotent; never raises. Writes after this
        are counted as failures rather than reopening a connection."""
        with self._lock:
            self._closed = True
            self._drop(invalidate=False)

    # -- internals ------------------------------------------------------------

    def _execute(self, params: dict[str, Any]) -> None:
        if self._conn is None:
            self._conn = self._open()
        self._conn.execute(self._sql, params)

    def _open(self) -> Any:
        password = self._tokens.token()
        connect = self._connect
        if connect is None:
            import psycopg

            connect = psycopg.connect
        statement_ms = max(1, int(self.statement_timeout_s * 1000))
        idle_s = max(1, int(self.statement_timeout_s))
        conn = connect(
            host=self.host,
            port=self.port,
            dbname=self.database,
            user=self.user,
            password=password,
            sslmode=self.sslmode,
            connect_timeout=max(1, round(self.connect_timeout_s)),
            application_name=APPLICATION_NAME,
            # A peer that vanishes mid-statement never answers, and
            # statement_timeout is enforced by that peer. These bound it from
            # this side: unacknowledged data, and silence on an idle socket.
            # libpq ignores all four on a Unix socket.
            tcp_user_timeout=statement_ms,
            keepalives=1,
            keepalives_idle=idle_s,
            keepalives_interval=1,
            keepalives_count=2,
            autocommit=True,
        )
        self.connects += 1
        try:
            # A bound set_config rather than `SET statement_timeout = ...`,
            # which cannot take a parameter; and a session setting rather than
            # an `options=-c ...` startup parameter, which a connection pooler
            # in front of Postgres may refuse.
            conn.execute(
                "SELECT set_config('statement_timeout', %s, false)", (f"{statement_ms}ms",)
            )
        except Exception:
            _close_quietly(conn)
            raise
        return conn

    def _drop(self, *, invalidate: bool = True) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            _close_quietly(conn)
        if invalidate:
            # The token may be why the connection failed; the next connect
            # should not present it again. Duck-typed: a provider without a
            # cache has nothing to forget.
            forget = getattr(self._tokens, "invalidate", None)
            if callable(forget):
                try:
                    forget()
                except Exception:  # noqa: BLE001
                    log.debug("token invalidate failed", exc_info=True)

    def _failed(self, run_id: str, status: str, exc: BaseException) -> bool:
        self.failures += 1
        self.last_error = _describe(exc)
        log.info(
            "could not report %s -> %s to Lakebase (%s); the run_events part file on "
            "the durable path still carries it",
            run_id,
            status,
            self.last_error,
        )
        return False


def _describe(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    return f"{type(exc).__name__}: {text[0]}" if text else type(exc).__name__


def _close_quietly(conn: Any) -> None:
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        log.debug("closing the Lakebase connection failed", exc_info=True)


def from_config(
    cfg: JobConfig,
    *,
    model: str | None = None,
    job_run_id: str | None = None,
    connect: Callable[..., Any] | None = None,
    connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
    statement_timeout_s: float = DEFAULT_STATEMENT_TIMEOUT_S,
) -> LakebaseStatusWriter | None:
    """The run's writer, or None when the job has no Lakebase identity.

    Reads the secret scope ONCE, here — via :func:`job.auth.m2m_from_secrets`,
    whose provider then caches tokens for the run — and takes the Postgres user
    from that same read (the principal's client id). Nothing is read when the
    configuration is incomplete.

    ``model`` defaults to ``cfg.model_spec`` and ``job_run_id`` to
    ``cfg.job_run_id``, so every write carries both.

    None is "not configured", never a failure: a job without a Lakebase
    identity runs exactly as before, with ``run_events`` as its record. One
    info line says which names were missing. Never raises.
    """
    missing = [
        name
        for name, value in (
            ("DBX_LAKEBASE_HOST", cfg.lakebase_host),
            ("DBX_LAKEBASE_OAUTH_SECRET_SCOPE", cfg.lakebase_secret_scope),
            ("DBX_LAKEBASE_OAUTH_CLIENT_ID_KEY", cfg.lakebase_client_id_key),
            ("DBX_LAKEBASE_OAUTH_SECRET_KEY", cfg.lakebase_secret_key),
            ("DATABRICKS_HOST", cfg.workspace_host),
        )
        if not value
    ]
    if missing:
        log.info(
            "no Lakebase status writer: %s not set; run_status will not be written by "
            "this job (run_events still records every transition)",
            ", ".join(missing),
        )
        return None

    try:
        tokens = auth.m2m_from_secrets(
            cfg.workspace_host,
            cfg.lakebase_secret_scope,
            cfg.lakebase_client_id_key,
            cfg.lakebase_secret_key,
        )
    except Exception as exc:  # noqa: BLE001 - never a reason to fail a run
        log.info("no Lakebase status writer: reading its credential failed (%s)", exc)
        return None
    if tokens is None:
        log.info(
            "no Lakebase status writer: could not read both %s and %s from secret "
            "scope %s; run_status will not be written by this job",
            cfg.lakebase_client_id_key,
            cfg.lakebase_secret_key,
            cfg.lakebase_secret_scope,
        )
        return None

    assert cfg.lakebase_host is not None  # checked above; for the type checker
    try:
        return LakebaseStatusWriter(
            cfg.lakebase_host,
            user=tokens.client_id,
            token_provider=tokens,
            port=cfg.lakebase_port,
            database=cfg.lakebase_database,
            schema=cfg.lakebase_schema,
            model=model if model is not None else cfg.model_spec,
            job_run_id=job_run_id if job_run_id is not None else cfg.job_run_id,
            connect=connect,
            connect_timeout_s=connect_timeout_s,
            statement_timeout_s=statement_timeout_s,
        )
    except Exception as exc:  # noqa: BLE001 - e.g. an unsafe schema name
        log.info("no Lakebase status writer: %s", _describe(exc))
        return None
