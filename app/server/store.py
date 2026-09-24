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

**What this store does not do: enforce the account's concurrency ceiling.**
An earlier design reserved a slot here at launch, in an advisory-locked
count-and-claim transaction. Nothing called it — a scheduled run never passes
through the app, so a ceiling counted on one route was counting the wrong
number — and it was removed in v5. Databricks holds the ceiling itself: every
``resources/model_*.job.yml`` sets ``queue.enabled``, so a sixth concurrent
task waits rather than failing. See ``docs/v4-rewrite-plan.md``, "Run state".

What Postgres still buys over Delta is a primary key on ``run_id``, which is
what lets :meth:`PostgresRunStore.set_status`'s upsert create the row on first
write and update it on every one after.

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
from typing import Any

from shared.envelope import TERMINAL_STATUSES, RunStatus, now_ms

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_SCHEMA",
    "RunRecord",
    "PostgresRunStore",
    "UnsafeSchemaName",
    "qualified",
    "schema_sql",
    "TERMINAL_SQL_LIST",
]

#: The terminal statuses as a SQL list literal, for the partial index over
#: runs that have not finished.
#:
#: This is one of the few places entitled to use `TERMINAL_STATUSES` now that a
#: status is an open string: the run store deals in the platform's own six, and
#: a model-defined status never reaches this column. Anything asking "is this
#: MESSAGE the last one" wants `StatusMessage.terminal` instead.
TERMINAL_SQL_LIST = ", ".join(f"'{s}'" for s in sorted(TERMINAL_STATUSES))


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
