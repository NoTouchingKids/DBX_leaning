"""Run state: the one piece of this platform that is OLTP-shaped.

Everything else the app touches is append-only and analytical — logs,
progress, events, results — and belongs in Delta. ``run_status`` is not: it
is one row per run, updated on every transition, read by point-lookup, and
counted against a concurrency ceiling. Delta is the wrong shape for all
three, and reading it means waking a SQL warehouse whose cost is *uptime*.

So it moves to Lakebase (managed Postgres), which
``docs/free-edition-constraints.md`` already earmarked for exactly this:
"Real fallback for OLTP-shaped state (`run_status`) and for multi-worker
fan-out via LISTEN/NOTIFY".

Two implementations behind one interface, chosen at startup:

- :class:`PostgresRunStore` when Lakebase is configured.
- :class:`WarehouseRunStore` otherwise — today's behaviour, unchanged, so a
  deployment is never blocked on provisioning a database.

Two things the Postgres one fixes that the warehouse one structurally cannot:

- **A duplicate ``run_id`` is refused** rather than silently producing two
  registry rows for one run. A primary key; Delta has none.
- **The concurrency ceiling is checked and the slot taken atomically.** The
  warehouse version counts, then launches, then inserts — two triggers
  arriving together both see room and both launch, straight past the
  account's 5-task limit. There is no way to write that correctly without a
  transaction.

Connections are opened per operation rather than pooled. That is a
deliberate first-cut choice: the volume is a handful of statements per run,
and Lakebase authenticates with a short-lived OAuth token, so resolving the
credential fresh on each connect makes rotation a non-issue instead of a
pool-invalidation problem. Add a pool when the volume justifies the
complexity, not before.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from shared.envelope import TERMINAL_STATUSES, RunStatus, now_ms

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_SCHEMA",
    "RunRecord",
    "PostgresRunStore",
    "SlotDenied",
    "DuplicateRun",
    "UnsafeSchemaName",
    "qualified",
    "schema_sql",
    "TERMINAL_SQL_LIST",
]

#: The terminal statuses as a SQL list literal, for counting what is still
#: active against the concurrency ceiling.
#:
#: This is one of the few places entitled to use `TERMINAL_STATUSES` now that a
#: status is an open string: the run store deals in the platform's own six, and
#: a model-defined status never reaches this column. Anything asking "is this
#: MESSAGE the last one" wants `StatusMessage.terminal` instead.
TERMINAL_SQL_LIST = ", ".join(f"'{s}'" for s in sorted(TERMINAL_STATUSES))


class SlotDenied(RuntimeError):
    """The account's concurrent-run ceiling is already taken."""

    def __init__(self, active: int, ceiling: int) -> None:
        super().__init__(
            f"{active} runs already active and the account ceiling is {ceiling} "
            f"concurrent job tasks; wait for one to finish"
        )
        self.active = active
        self.ceiling = ceiling


class DuplicateRun(RuntimeError):
    """That run_id is already registered."""


@dataclass(frozen=True)
class RunRecord:
    """One run's current state. The object the rest of the app passes around,
    instead of a bare dict whose keys everyone has to remember."""

    run_id: str
    model: str
    status: str = RunStatus.QUEUED
    job_run_id: str | None = None
    detail: str | None = None
    started_ts: int = 0
    updated_ts: int = 0
    requested_by: str | None = None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> RunRecord:
        # A status is a plain string now, so there is nothing to coerce and
        # nothing to reject. An unfamiliar value is carried through rather than
        # rewritten to FAILED, which is what the enum forced and which lost the
        # only evidence of what actually happened. A blank is still a data
        # problem and says so.
        status = str(row.get("status") or "")
        if not status:
            log.warning("run %s has no status", row.get("run_id"))
            status = RunStatus.FAILED
        return cls(
            run_id=str(row["run_id"]),
            model=str(row.get("model") or ""),
            status=status,
            job_run_id=None if row.get("job_run_id") is None else str(row["job_run_id"]),
            detail=row.get("detail"),
            started_ts=int(row.get("started_ts") or 0),
            updated_ts=int(row.get("updated_ts") or 0),
            requested_by=row.get("requested_by"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "model": self.model,
            "status": self.status,
            "job_run_id": self.job_run_id,
            "detail": self.detail,
            "started_ts": self.started_ts,
            "updated_ts": self.updated_ts,
            "requested_by": self.requested_by,
        }


@runtime_checkable
class RunStore(Protocol):
    name: str

    async def ensure_schema(self) -> None: ...

    async def claim_slot(
        self, run_id: str, *, model: str, ceiling: int, requested_by: str | None = None
    ) -> RunRecord:
        """Reserve a slot and register the run, or raise.

        Raises :class:`SlotDenied` if the ceiling is already taken and
        :class:`DuplicateRun` if the id exists. Where the implementation can,
        both happen in one transaction with the insert.
        """

    async def attach_job_run(self, run_id: str, job_run_id: str | int) -> None: ...

    async def release_slot(self, run_id: str) -> None:
        """Undo a claim whose launch then failed."""

    async def set_status(self, run_id: str, status: str, *, detail: str | None = None) -> None: ...

    async def get(self, run_id: str) -> RunRecord | None: ...

    # Named list_runs, not list: a method called `list` shadows the builtin
    # inside the class body, so `-> list[RunRecord]` would resolve to the
    # method rather than the type.
    async def list_runs(
        self, *, limit: int = 50, status: str | None = None, model: str | None = None
    ) -> list[RunRecord]: ...

    async def active_count(self) -> int: ...

    async def non_terminal(self, limit: int = 200) -> list[RunRecord]: ...

    async def close(self) -> None: ...


# --------------------------------------------------------------------------
# Postgres / Lakebase
# --------------------------------------------------------------------------

#: Postgres identifier, for the one thing here that cannot be a bound
#: parameter. A schema name is an identifier, not a value.
_PG_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Where `run_status` lives inside the Lakebase database.
#:
#: NOT `public`, and that is not tidiness. Since PostgreSQL 15 the `public`
#: schema no longer grants CREATE to `PUBLIC`, so a role that is not the
#: database owner — which the app's service principal generally is not — gets
#: `permission denied for schema public` the first time `ensure_schema()`
#: runs. Owning a schema of its own is the difference between a deploy that
#: works and one that reports `lakebase` degraded for a reason nobody expects.
#:
#: It also mirrors the Unity Catalog side, where everything is in
#: `<catalog>.dbx_leaning` rather than loose in `default`.
DEFAULT_SCHEMA = "dbx_leaning"


class UnsafeSchemaName(ValueError):
    """A schema name that will not be interpolated into SQL."""


def qualified(schema: str) -> str:
    """`schema.run_status`, vetted.

    Every statement qualifies the table rather than relying on `search_path`.
    A search path is per-session state: it would have to be set on each of the
    connections this store opens per operation, and one that silently reverts
    to `public` finds a DIFFERENT, empty table rather than failing — which is
    a far worse outcome than an error.
    """
    if not _PG_IDENTIFIER.match(schema):
        raise UnsafeSchemaName(
            f"{schema!r} is not a plain Postgres identifier; refusing to build SQL from it"
        )
    return f"{schema}.run_status"


def schema_sql(schema: str) -> str:
    table = qualified(schema)
    return f"""
CREATE SCHEMA IF NOT EXISTS {schema};

CREATE TABLE IF NOT EXISTS {table} (
    run_id       TEXT PRIMARY KEY,
    job_run_id   TEXT,
    model        TEXT   NOT NULL,
    status       TEXT   NOT NULL,
    detail       TEXT,
    started_ts   BIGINT NOT NULL,
    updated_ts   BIGINT NOT NULL,
    requested_by TEXT
);

-- Partial index: the ceiling check and reconciliation both ask only about
-- runs that have not finished, and finished runs are the overwhelming
-- majority once this has been live for a while.
CREATE INDEX IF NOT EXISTS run_status_active_idx
    ON {table} (updated_ts DESC)
    WHERE status NOT IN ({TERMINAL_SQL_LIST});

CREATE INDEX IF NOT EXISTS run_status_recent_idx ON {table} (updated_ts DESC);
"""


#: One well-known lock id, so every ceiling check serialises against the
#: others. Arbitrary but fixed; changing it would let two app versions race.
_CEILING_LOCK_ID = 230825001


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
        self._table = qualified(schema)
        #: Awaited on every connection, when set. Lakebase's password is a
        #: short-lived OAuth token, so it cannot live in the DSN: baked in at
        #: startup it works for about an hour, and this app runs for up to 24.
        #: Resolving it here is the reason a connection is opened per
        #: operation rather than pooled. None means a static password (or
        #: none at all) is already in the DSN — the local dev stack, or an
        #: instance with `enable_pg_native_login` turned on.
        self._password_provider = password_provider
        self._connect = connect  # injectable for tests
        #: What the server said it is, read once at `ensure_schema`. Reported
        #: by `/healthz` because the alternative is asserting it, and this
        #: repo asserted wrong: it claimed "Lakebase runs PostgreSQL 18" while
        #: a real instance came back `PG_VERSION_16`, the default. The version
        #: is chosen at creation and immutable after, so a deployment can
        #: legitimately be on either. One string from the server settles it,
        #: and costs a query on a connection already being opened.
        self.server_version: str | None = None

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

    async def ensure_schema(self) -> None:
        conn = await self._conn()
        try:
            await conn.execute(schema_sql(self._schema))
            self.server_version = await self._read_server_version(conn)
        finally:
            await conn.close()

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

    async def claim_slot(
        self, run_id: str, *, model: str, ceiling: int, requested_by: str | None = None
    ) -> RunRecord:
        now = now_ms()
        conn = await self._conn()
        try:
            await conn.set_autocommit(False)
            async with conn.cursor() as cur:
                # Serialise every ceiling check against every other one. Without
                # this, two triggers both count 4 and both insert a 5th.
                await cur.execute("SELECT pg_advisory_xact_lock(%s)", (_CEILING_LOCK_ID,))

                await cur.execute(
                    f"SELECT COUNT(*) FROM {self._table} WHERE status NOT IN ({TERMINAL_SQL_LIST})"
                )
                active = int((await cur.fetchone())[0])
                if active >= ceiling:
                    await conn.rollback()
                    raise SlotDenied(active, ceiling)

                await cur.execute(
                    f"""
                    INSERT INTO {self._table}
                        (run_id, job_run_id, model, status, detail,
                         started_ts, updated_ts, requested_by)
                    VALUES (%s, NULL, %s, %s, NULL, %s, %s, %s)
                    ON CONFLICT (run_id) DO NOTHING
                    """,
                    (run_id, model, RunStatus.QUEUED, now, now, requested_by),
                )
                if cur.rowcount == 0:
                    await conn.rollback()
                    raise DuplicateRun(f"run_id {run_id!r} is already registered")
            await conn.commit()
        finally:
            await conn.close()

        return RunRecord(
            run_id=run_id,
            model=model,
            status=RunStatus.QUEUED,
            started_ts=now,
            updated_ts=now,
            requested_by=requested_by,
        )

    async def attach_job_run(self, run_id: str, job_run_id: str | int) -> None:
        conn = await self._conn()
        try:
            await conn.execute(
                f"UPDATE {self._table} SET job_run_id = %s, updated_ts = %s WHERE run_id = %s",
                (str(job_run_id), now_ms(), run_id),
            )
        finally:
            await conn.close()

    async def release_slot(self, run_id: str) -> None:
        conn = await self._conn()
        try:
            # Only a run that never started: never delete one that has begun
            # reporting, or a late status write would resurrect a ghost row.
            await conn.execute(
                f"DELETE FROM {self._table} WHERE run_id = %s AND status = %s",
                (run_id, RunStatus.QUEUED),
            )
        finally:
            await conn.close()

    async def set_status(self, run_id: str, status: str, *, detail: str | None = None) -> None:
        value = str(status)
        conn = await self._conn()
        try:
            await conn.execute(
                f"""
                INSERT INTO {self._table}
                    (run_id, model, status, detail, started_ts, updated_ts)
                VALUES (%s, '', %s, %s, %s, %s)
                ON CONFLICT (run_id) DO UPDATE
                SET status = EXCLUDED.status,
                    detail = EXCLUDED.detail,
                    updated_ts = EXCLUDED.updated_ts
                """,
                (run_id, value, detail, now_ms(), now_ms()),
            )
        finally:
            await conn.close()

    async def get(self, run_id: str) -> RunRecord | None:
        conn = await self._conn()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT {_COLUMNS} FROM {self._table} WHERE run_id = %s", (run_id,)
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
                    "ORDER BY updated_ts DESC LIMIT %s",
                    tuple(params),
                )
                return [RunRecord.from_row(_zip(r)) for r in await cur.fetchall()]
        finally:
            await conn.close()

    async def active_count(self) -> int:
        conn = await self._conn()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT COUNT(*) FROM {self._table} WHERE status NOT IN ({TERMINAL_SQL_LIST})"
                )
                return int((await cur.fetchone())[0])
        finally:
            await conn.close()

    async def non_terminal(self, limit: int = 200) -> list[RunRecord]:
        conn = await self._conn()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT {_COLUMNS} FROM {self._table} "
                    f"WHERE status NOT IN ({TERMINAL_SQL_LIST}) "
                    f"ORDER BY updated_ts DESC LIMIT %s",
                    (limit,),
                )
                return [RunRecord.from_row(_zip(r)) for r in await cur.fetchall()]
        finally:
            await conn.close()

    async def close(self) -> None:
        return None


_COLUMN_NAMES = (
    "run_id",
    "job_run_id",
    "model",
    "status",
    "detail",
    "started_ts",
    "updated_ts",
    "requested_by",
)
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
        clauses.append("status = %s")
        params.append(status)
    if model:
        clauses.append("model = %s")
        params.append(model)
    return (f"WHERE {' AND '.join(clauses)}" if clauses else ""), params


# --------------------------------------------------------------------------
# Warehouse (today's behaviour, kept so a deploy is never blocked on Lakebase)

# The warehouse-backed store that used to live here is gone. It existed so a
# deploy was never blocked on provisioning Lakebase; Lakebase is provisioned,
# and v4 takes the SQL warehouse off the app's live path entirely. The
# `RunStore` Protocol went with it — one implementation does not need an
# interface, and the seam it was holding open is not one v4 wants.
