"""Run state: the one piece of this platform that is OLTP-shaped.

Everything else the app touches is append-only and analytical — logs,
progress, events, results — and belongs in Delta. ``run_status`` is not: it
is one row per run, updated on every transition and read by point-lookup.
Delta is the wrong shape for both, and reading it means waking a SQL
warehouse whose cost is *uptime*.

So it lives in Lakebase (managed Postgres), which
``docs/free-edition-constraints.md`` earmarked for exactly this. One
implementation, :class:`PostgresRunStore`, and no interface in front of it:
the warehouse-backed store it used to share a ``RunStore`` Protocol with is
gone, and one implementation does not need a seam.

**Two tables.** ``run_status`` is current state, one row per run;
``run_status_history`` is every reported transition, append-only. One status
report writes both, in one statement (:data:`REPORT_SQL`, which lives in
``shared/run_state.py`` because the job's writer issues it too). The shape was signed
off on 2026-09-24 — ``docs/v5-implementation-plan.md``, Phase 2 item 1.

**This module creates nothing.** The DDL is ``lakebase_ddl/001_run_status.sql``
and ``002_run_status_history.sql``, applied out of band by a human or a deploy
step (Phase 2 item 3). At startup :meth:`PostgresRunStore.check_schema` reads
``information_schema`` and reports what is missing or different, and the app
reports that as degraded rather than creating it. This file holds no copy of
the DDL — only :data:`EXPECTED_COLUMNS` and :data:`EXPECTED_KEYS`, what the
check compares against — and ``tests/deploy/test_lakebase_ddl.py`` fails if
those drift from the ``.sql`` files. The files cannot simply be read at runtime:
``lakebase_ddl/`` is outside ``app/``, and nothing outside ``app/`` deploys.

**What this store does not do: enforce the account's concurrency ceiling.**
Databricks holds the ceiling itself: every ``resources/model_*.job.yml`` sets
``queue.enabled``, so a sixth concurrent task waits rather than failing. See
``docs/v4-rewrite-plan.md``, "Run state".

Connections are opened per operation rather than pooled. That is a
deliberate first-cut choice: the volume is a handful of statements per run,
and Lakebase authenticates with a short-lived OAuth token, so resolving the
credential fresh on each connect makes rotation a non-issue instead of a
pool-invalidation problem. Add a pool when the volume justifies the
complexity, not before.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, NamedTuple

from shared.envelope import RunStatus
from shared.run_state import (
    DEFAULT_SCHEMA,
    HISTORY_TABLE,
    REPORT_SQL,
    STATUS_TABLE,
    UnsafeSchemaName,
    report_params,
    report_sql,
    vet_schema,
)

log = logging.getLogger(__name__)

__all__ = [
    "Column",
    "DDL_FILES",
    "DEFAULT_SCHEMA",
    "EXPECTED_COLUMNS",
    "EXPECTED_KEYS",
    "HISTORY_TABLE",
    "HistoryRecord",
    "PostgresRunStore",
    "REPORT_SQL",
    "RunRecord",
    "STATUS_TABLE",
    "UnsafeSchemaName",
    "qualified",
]

#: Which DDL file creates each table — named in the degraded reason, so the
#: message says what to apply rather than only what is wrong.
DDL_FILES = {
    STATUS_TABLE: "lakebase_ddl/001_run_status.sql",
    HISTORY_TABLE: "lakebase_ddl/002_run_status_history.sql",
}


class Column(NamedTuple):
    #: As `information_schema.columns.data_type` spells it: BIGSERIAL is
    #: `bigint` there, TEXT is `text`.
    data_type: str
    nullable: bool


#: What the startup check expects, per table. The canonical DDL is the `.sql`
#: files; this is the part of it the app depends on, checked against the live
#: database at startup and against the files by tests/deploy/test_lakebase_ddl.py.
#:
#: Extra columns are tolerated: a column added out of band ahead of an app
#: that reads it is an additive migration, not a mismatch.
EXPECTED_COLUMNS: dict[str, dict[str, Column]] = {
    STATUS_TABLE: {
        "run_id": Column("text", False),
        "job_run_id": Column("text", True),
        "model": Column("text", False),
        "status": Column("text", False),
        "terminal": Column("boolean", False),
        "detail": Column("text", True),
        "seq": Column("bigint", False),
        "updated_at": Column("bigint", False),
    },
    HISTORY_TABLE: {
        "id": Column("bigint", False),
        "run_id": Column("text", False),
        "seq": Column("bigint", False),
        "status": Column("text", False),
        "terminal": Column("boolean", False),
        "detail": Column("text", True),
        "ts": Column("bigint", False),
    },
}

#: The conflict targets :data:`REPORT_SQL` names. A table with the right
#: columns and no such key fails EVERY write with "there is no unique or
#: exclusion constraint matching the ON CONFLICT specification" — so it is
#: checked, not assumed.
EXPECTED_KEYS: dict[str, tuple[str, ...]] = {
    STATUS_TABLE: ("run_id",),
    HISTORY_TABLE: ("run_id", "seq"),
}


@dataclass(frozen=True)
class RunRecord:
    """One run's current state. The object the rest of the app passes around,
    instead of a bare dict whose keys everyone has to remember.

    ``terminal`` is a stored column, as the producer stated it — not derived
    from a list of status strings, which could not answer for a model-defined
    status.
    """

    run_id: str
    model: str
    status: str
    terminal: bool
    seq: int
    updated_at: int
    job_run_id: str | None = None
    detail: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> RunRecord:
        # A status is a plain string, so there is nothing to coerce and
        # nothing to reject. An unfamiliar value is carried through rather than
        # rewritten. A blank is still a data problem and says so.
        status = str(row.get("status") or "")
        if not status:
            log.warning("run %s has no status", row.get("run_id"))
            status = RunStatus.FAILED
        return cls(
            run_id=str(row["run_id"]),
            model=str(row.get("model") or ""),
            status=status,
            terminal=bool(row.get("terminal")),
            seq=int(row.get("seq") or 0),
            updated_at=int(row.get("updated_at") or 0),
            job_run_id=None if row.get("job_run_id") is None else str(row["job_run_id"]),
            detail=row.get("detail"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "model": self.model,
            "status": self.status,
            "terminal": self.terminal,
            "seq": self.seq,
            "updated_at": self.updated_at,
            "job_run_id": self.job_run_id,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class HistoryRecord:
    """One reported transition, as it arrived — including one the current-row
    upsert refused as stale."""

    run_id: str
    seq: int
    status: str
    terminal: bool
    ts: int
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "seq": self.seq,
            "status": self.status,
            "terminal": self.terminal,
            "ts": self.ts,
            "detail": self.detail,
        }


# --------------------------------------------------------------------------
# Postgres / Lakebase
# --------------------------------------------------------------------------

#: `DEFAULT_SCHEMA` (``dbx_leaning``, not ``public`` — see its note), the
#: identifier check behind :func:`qualified`, the table names and
#: :data:`REPORT_SQL` itself all live in ``shared/run_state.py`` and are
#: re-exported from here. The job's own Lakebase writer (``job/lakebase.py``)
#: must issue exactly the same statement, and the job does not import
#: ``server`` — so the text lives where both can reach it, once.


def qualified(schema: str, table: str = STATUS_TABLE) -> str:
    """`schema.table`, vetted.

    Every statement qualifies the table rather than relying on `search_path`.
    A search path is per-session state: it would have to be set on each of the
    connections this store opens per operation, and one that silently reverts
    to `public` finds a DIFFERENT, empty table rather than failing — which is
    a far worse outcome than an error.
    """
    vet_schema(schema)
    if table not in EXPECTED_COLUMNS:
        raise ValueError(f"{table!r} is not a table this store knows")
    return f"{schema}.{table}"


_COLUMNS_SQL = """
SELECT table_name::text, column_name::text, data_type::text, is_nullable::text
FROM information_schema.columns
WHERE table_schema::text = %s::text AND table_name::text = ANY(%s::text[])
"""

_KEYS_SQL = """
SELECT tc.table_name::text,
       array_agg(kcu.column_name::text ORDER BY kcu.ordinal_position)
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON kcu.constraint_schema = tc.constraint_schema
 AND kcu.constraint_name = tc.constraint_name
 AND kcu.table_name = tc.table_name
WHERE tc.table_schema::text = %s::text
  AND tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE')
GROUP BY tc.table_name, tc.constraint_name
"""


class PostgresRunStore:
    """Lakebase. Standard Postgres — nothing here is Databricks-specific
    except how the password is obtained."""

    name = "postgres"

    def __init__(
        self, dsn: str, *, schema: str = DEFAULT_SCHEMA, password_provider=None, connect=None
    ) -> None:
        self._dsn = dsn
        #: Vetted at construction, not at first use: a bad schema name should
        #: fail while the app is starting and can report it, not on the first
        #: trigger of the day.
        self._schema = schema
        self._table = qualified(schema, STATUS_TABLE)
        self._history = qualified(schema, HISTORY_TABLE)
        self._report_sql = report_sql(schema)
        #: Awaited on every connection, when set. Lakebase's password is a
        #: short-lived OAuth token, so it cannot live in the DSN: baked in at
        #: startup it works for about an hour, and this app runs for up to 24.
        #: Resolving it here is the reason a connection is opened per
        #: operation rather than pooled. None means a static password (or
        #: none at all) is already in the DSN — the local dev stack, or an
        #: instance with `enable_pg_native_login` turned on.
        self._password_provider = password_provider
        self._connect = connect  # injectable for tests
        #: What the server said it is, read once at `check_schema`. Reported
        #: by `/healthz` because the alternative is asserting it, and this
        #: repo asserted wrong: it claimed "Lakebase runs PostgreSQL 18" while
        #: a real instance came back `PG_VERSION_16`, the default. The version
        #: is chosen at creation and immutable after, so a deployment can
        #: legitimately be on either.
        self.server_version: str | None = None

    @property
    def schema(self) -> str:
        return self._schema

    async def _conn(self):
        if self._connect is not None:
            return await self._connect()
        import psycopg

        params: dict[str, Any] = {"autocommit": True}
        if self._password_provider is not None:
            # A keyword overrides whatever the DSN says, so the DSN never has
            # to carry a credential at all.
            params["password"] = await self._password_provider()
        return await psycopg.AsyncConnection.connect(self._dsn, **params)

    async def check_schema(self) -> list[str]:
        """What is missing or different about the tables this store needs.

        An empty list means the schema is as expected. **Creates nothing** —
        DDL is applied out of band (``lakebase_ddl/``, ``deploy/README.md``);
        a store that created its own tables would hide exactly the drift this
        exists to report. Raises only when the database cannot be reached at
        all, which is a different fault with a different fix.

        Also records the server version, on the connection already open.
        """
        conn = await self._conn()
        try:
            self.server_version = await self._read_server_version(conn)
            return await self._schema_problems(conn)
        finally:
            await conn.close()

    async def _schema_problems(self, conn) -> list[str]:
        cur = await conn.execute(_COLUMNS_SQL, (self._schema, list(EXPECTED_COLUMNS)))
        actual: dict[str, dict[str, Column]] = {}
        for table, column, data_type, is_nullable in await cur.fetchall():
            actual.setdefault(table, {})[column] = Column(data_type, is_nullable == "YES")

        cur = await conn.execute(_KEYS_SQL, (self._schema,))
        keys: dict[str, set[tuple[str, ...]]] = {}
        for table, columns in await cur.fetchall():
            keys.setdefault(table, set()).add(tuple(columns))

        problems: list[str] = []
        for table, expected in EXPECTED_COLUMNS.items():
            name = f"{self._schema}.{table}"
            have = actual.get(table)
            if not have:
                problems.append(f"{name} does not exist (apply {DDL_FILES[table]})")
                continue
            for column, want in expected.items():
                got = have.get(column)
                if got is None:
                    problems.append(f"{name} has no column {column} ({want.data_type})")
                elif got != want:
                    problems.append(
                        f"{name}.{column} is {_describe(got)}, expected {_describe(want)}"
                    )
            if EXPECTED_KEYS[table] not in keys.get(table, set()):
                problems.append(
                    f"{name} has no primary key or unique constraint on "
                    f"({', '.join(EXPECTED_KEYS[table])}), which every write's "
                    "ON CONFLICT names"
                )
        return problems

    @staticmethod
    async def _read_server_version(conn) -> str | None:
        """Never fatal. A store that works but cannot report its version is
        strictly better than a startup that fails over a diagnostic."""
        try:
            cur = await conn.execute("SHOW server_version")
            row = await cur.fetchone()
        except Exception:  # noqa: BLE001
            log.debug("could not read the Postgres server version", exc_info=True)
            return None
        return str(row[0]) if row else None

    async def set_status(
        self,
        run_id: str,
        status: str,
        *,
        seq: int,
        terminal: bool,
        ts: int,
        detail: str | None = None,
        model: str = "",
        job_run_id: str | None = None,
    ) -> None:
        """Report one status message: upsert the current row (seq-guarded) and
        append it to history (deduped on run_id, seq). See :data:`REPORT_SQL`.

        ``seq``, ``terminal`` and ``ts`` are the MESSAGE's, never this
        process's — the guard compares the message's own clock.

        **No production caller in the app, deliberately.** Since v5 the job
        is the sole writer of ``run_status`` (``job/lakebase.py``); the app
        only reads it. This stays because it is the app-side half of the
        equivalence test that pins both writers to the one shared statement
        in ``shared/run_state.py``, and because a test seeding rows through
        the real statement beats one that hand-writes SQL.
        """
        conn = await self._conn()
        try:
            await conn.execute(
                self._report_sql,
                report_params(
                    run_id,
                    status,
                    seq=seq,
                    terminal=terminal,
                    ts=ts,
                    detail=detail,
                    model=model,
                    job_run_id=job_run_id,
                ),
            )
        finally:
            await conn.close()

    async def get(self, run_id: str) -> RunRecord | None:
        conn = await self._conn()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT {_COLUMNS} FROM {self._table} WHERE run_id = %s::text", (run_id,)
                )
                row = await cur.fetchone()
                return RunRecord.from_row(_zip(row)) if row else None
        finally:
            await conn.close()

    async def list_runs(
        self, *, limit: int = 50, status: str | None = None, model: str | None = None
    ) -> list[RunRecord]:
        # Clause and parameter built together in one pass. Branching on which
        # filters are set gives 2^n statements to keep in step, and the
        # placeholders here are positional — one that drifts out of order
        # against its value is a filter that silently matches the wrong thing.
        where, params = _filters(status=status, model=model)
        params.append(limit)
        conn = await self._conn()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT {_COLUMNS} FROM {self._table} {where} "
                    "ORDER BY updated_at DESC LIMIT %s::bigint",
                    tuple(params),
                )
                return [RunRecord.from_row(_zip(r)) for r in await cur.fetchall()]
        finally:
            await conn.close()

    async def history(self, run_id: str) -> list[HistoryRecord]:
        """Every reported transition for one run, in the order the job issued
        them (``seq``). Empty for a run never reported, not an error."""
        conn = await self._conn()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT run_id, seq, status, terminal, ts, detail FROM {self._history} "
                    "WHERE run_id = %s::text ORDER BY seq, id",
                    (run_id,),
                )
                return [
                    HistoryRecord(
                        run_id=str(r[0]),
                        seq=int(r[1]),
                        status=str(r[2]),
                        terminal=bool(r[3]),
                        ts=int(r[4]),
                        detail=r[5],
                    )
                    for r in await cur.fetchall()
                ]
        finally:
            await conn.close()

    async def close(self) -> None:
        return None


def _describe(column: Column) -> str:
    return f"{column.data_type} {'NULL' if column.nullable else 'NOT NULL'}"


_COLUMN_NAMES = tuple(EXPECTED_COLUMNS[STATUS_TABLE])
_COLUMNS = ", ".join(_COLUMN_NAMES)


def _zip(row) -> dict[str, Any]:
    return dict(zip(_COLUMN_NAMES, row, strict=True))


def _filters(*, status: str | None, model: str | None) -> tuple[str, list[Any]]:
    """A WHERE clause and its parameters, built as one thing.

    Every value is a placeholder; nothing here is interpolated. The column
    names are literals in this function, not caller input.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status = %s::text")
        params.append(status)
    if model:
        clauses.append("model = %s::text")
        params.append(model)
    return (f"WHERE {' AND '.join(clauses)}" if clauses else ""), params
