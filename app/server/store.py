"""Run state: the one piece of this platform that is OLTP-shaped.

Everything else the app touches is append-only, high-volume and analytical —
logs, progress, events, results — and belongs in Delta. ``run_status`` is
not: it is one row per run, updated on every transition, read by
point-lookup, and counted against a concurrency ceiling. Delta is the wrong
shape for all three, and reading it means waking a SQL warehouse whose cost
is *uptime*.

One append-only table sits beside it — ``run_status_history``, a handful of
rows per run rather than thousands. It is a separate table and not
``run_status`` made append-only, which would cost exactly the two properties
below; :func:`history_schema_sql` spells that out.

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
    "RunStore",
    "StatusTransition",
    "PostgresRunStore",
    "WarehouseRunStore",
    "SlotDenied",
    "DuplicateRun",
    "UnsafeSchemaName",
    "qualified",
    "qualified_history",
    "schema_sql",
    "history_schema_sql",
    "TERMINAL_SQL_LIST",
]

#: The terminal statuses as a SQL list literal. Built from the enum rather
#: than typed out, so adding a status cannot leave this behind.
TERMINAL_SQL_LIST = ", ".join(
    f"'{s.value}'" for s in sorted(TERMINAL_STATUSES, key=lambda s: s.value)
)


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
    status: RunStatus
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
        raw = row.get("status")
        try:
            status = RunStatus(raw)
        except ValueError:
            # A status nobody recognises is a data problem, not a crash: keep
            # the run visible and let a human see the odd value.
            log.warning("run %s has unknown status %r", row.get("run_id"), raw)
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
            "status": self.status.value,
            "job_run_id": self.job_run_id,
            "detail": self.detail,
            "started_ts": self.started_ts,
            "updated_ts": self.updated_ts,
            "requested_by": self.requested_by,
        }


@dataclass(frozen=True)
class StatusTransition:
    """One row of ``run_status_history``: a transition that was reported, not
    the state the run is in now.

    ``RunRecord`` is the answer to "what is this run doing"; a list of these is
    the answer to "how did it get there", which the current-state row cannot
    give because every transition overwrites the last one.

    ``status`` stays a plain string rather than becoming a :class:`RunStatus`.
    ``RunRecord.from_row`` maps an unrecognised value onto ``FAILED`` so a
    junk row still renders — the right call for current state, and the wrong
    one here. This is an audit record: rewriting a status nobody recognises as
    ``FAILED`` invents a transition that never happened, and hides the data
    problem that produced it.
    """

    run_id: str
    status: str
    ts: int
    seq: int | None = None
    detail: str | None = None
    recorded_by: str = "job"
    #: Insertion order, from the table's BIGSERIAL. The tiebreaker for rows
    #: sharing a ``ts``: epoch milliseconds collide routinely — QUEUED and
    #: RUNNING inside one millisecond is an ordinary fast start — and two
    #: transitions in the wrong order is a history that reads as a lie.
    id: int = 0

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> StatusTransition:
        seq = row.get("seq")
        return cls(
            run_id=str(row["run_id"]),
            status=str(row["status"]),
            ts=int(row.get("ts") or 0),
            seq=None if seq is None else int(seq),
            detail=row.get("detail"),
            recorded_by=str(row.get("recorded_by") or "job"),
            id=int(row.get("id") or 0),
        )


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

    async def set_status(
        self, run_id: str, status: RunStatus | str, *, detail: str | None = None
    ) -> None: ...

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


def _vetted(schema: str) -> str:
    """One gate for the one thing here that cannot be a bound parameter.

    Every qualifier goes through this, so adding a table cannot add a way to
    skip the check.
    """
    if not _PG_IDENTIFIER.match(schema):
        raise UnsafeSchemaName(
            f"{schema!r} is not a plain Postgres identifier; refusing to build SQL from it"
        )
    return schema


def qualified(schema: str) -> str:
    """`schema.run_status`, vetted.

    Every statement qualifies the table rather than relying on `search_path`.
    A search path is per-session state: it would have to be set on each of the
    connections this store opens per operation, and one that silently reverts
    to `public` finds a DIFFERENT, empty table rather than failing — which is
    a far worse outcome than an error.
    """
    return f"{_vetted(schema)}.run_status"


def qualified_history(schema: str) -> str:
    """`schema.run_status_history`, vetted the same way.

    A separate table, deliberately. See :func:`history_schema_sql` for why
    `run_status` did not simply become append-only instead.
    """
    return f"{_vetted(schema)}.run_status_history"


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


def history_schema_sql(schema: str) -> str:
    """The append-only transition log that sits BESIDE `run_status`.

    Not `run_status` made append-only, and the difference is the whole reason
    Postgres holds this table at all:

    - **The primary key on `run_id`.** Many rows per run and there is nothing
      left to conflict on, so `ON CONFLICT (run_id) DO UPDATE` — the job's
      upsert with its `updated_ts` guard, and this store's `set_status` — has
      no target, and a duplicate `run_id` stops being refusable. That is
      precisely the Delta failure the move to Postgres fixed: two registry
      rows for one run and the reader taking whichever came back first.
    - **The transactional count-and-claim.** `claim_slot` counts non-terminal
      runs inside one advisory-locked transaction. Against an append-only
      table that count becomes a latest-row-per-run subquery, and one written
      slightly wrong over-counts finished runs and jams the account's 5-task
      ceiling shut, or under-counts and sails past it.

    So: current state keeps its shape, history gets its own table. Anyone
    tempted to "simplify" the two back into one loses both properties.

    Delta's `run_events` (`uc_ddl/001_core_tables.sql`) holds the same
    transitions durably and stays the record of truth. It is not a substitute
    for this: reading it means waking the SQL warehouse, whose cost is uptime,
    which is the exact spend this platform is built to avoid.
    """
    table = qualified_history(schema)
    return f"""
CREATE SCHEMA IF NOT EXISTS {schema};

CREATE TABLE IF NOT EXISTS {table} (
    id          BIGSERIAL PRIMARY KEY,
    run_id      TEXT   NOT NULL,
    seq         BIGINT,            -- the envelope seq of the status message, when it came from one
    status      TEXT   NOT NULL,
    detail      TEXT,
    ts          BIGINT NOT NULL,   -- epoch ms, when the transition happened
    recorded_by TEXT   NOT NULL DEFAULT 'job'
);

-- Retries are idempotent: the job may report the same status message twice
-- after a reconnect, and `seq` identifies it uniquely within a run. Partial,
-- because a writer with no envelope message behind it (the app, at slot-claim
-- time) has no seq and must still be able to append.
CREATE UNIQUE INDEX IF NOT EXISTS run_status_history_seq_idx
    ON {table} (run_id, seq) WHERE seq IS NOT NULL;

CREATE INDEX IF NOT EXISTS run_status_history_run_idx
    ON {table} (run_id, ts DESC);
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
        self._history = qualified_history(schema)
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
            # Both tables in ONE statement string, which Postgres runs as a
            # single implicit transaction — verified, not assumed: a failing
            # tail statement rolls the earlier CREATEs back. So the schema is
            # all-or-nothing. Two execute() calls would let `run_status`
            # succeed while the history table failed, leaving an app that is
            # up, healthy, and silently losing every transition the job
            # appends. Half a schema fails quietly; a missing one does not.
            await conn.execute(schema_sql(self._schema) + history_schema_sql(self._schema))
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
                    (run_id, model, RunStatus.QUEUED.value, now, now, requested_by),
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
                (run_id, RunStatus.QUEUED.value),
            )
        finally:
            await conn.close()

    async def set_status(
        self, run_id: str, status: RunStatus | str, *, detail: str | None = None
    ) -> None:
        value = status.value if isinstance(status, RunStatus) else str(status)
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

    async def history(self, run_id: str, *, limit: int = 500) -> list[StatusTransition]:
        """Every transition recorded for one run, oldest first.

        Read-only from here. The job writes this table over the Database REST
        API (`job/lakebase.py`), the same request that upserts the current
        row; the app reads it back.

        Deliberately NOT on the :class:`RunStore` Protocol. `run_status_history`
        is a Postgres table with no warehouse equivalent, and giving
        :class:`WarehouseRunStore` a version that returns `[]` would make "this
        deployment has no history table" indistinguishable from "this run had
        no transitions" — the same silent degradation as an app that ran the
        fallback store for weeks because nothing set `DBX_LAKEBASE_*`. A caller
        that wants this checks the store it has.

        Ordered newest-first in SQL and reversed here, which is not fussiness:
        `ORDER BY ts LIMIT n` truncates the *tail*, so a run with more
        transitions than the bound comes back missing its terminal one — the
        single row anyone reading a history is looking for.
        """
        conn = await self._conn()
        try:
            async with conn.cursor() as cur:
                # `limit` is bound as an integer, not spliced into the text.
                # A LIMIT is the one clause where a string-typed number is
                # rejected outright rather than quietly mis-comparing, but
                # `seq` and `ts` in this table are BIGINT for the reason the
                # rest of this codebase binds types: compared as strings,
                # "2" > "12".
                await cur.execute(
                    f"SELECT {_HISTORY_COLUMNS} FROM {self._history} "
                    "WHERE run_id = %s ORDER BY ts DESC, id DESC LIMIT %s",
                    (run_id, limit),
                )
                rows = await cur.fetchall()
        finally:
            await conn.close()
        return [StatusTransition.from_row(_zip_history(r)) for r in reversed(rows)]

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

#: Listed in the SELECT rather than `SELECT *`, so a column added to the table
#: cannot shift the tuple under `_zip_history` and silently re-label every row.
_HISTORY_COLUMN_NAMES = ("id", "run_id", "seq", "status", "detail", "ts", "recorded_by")
_HISTORY_COLUMNS = ", ".join(_HISTORY_COLUMN_NAMES)


def _zip(row) -> dict[str, Any]:
    return dict(zip(_COLUMN_NAMES, row, strict=True))


def _zip_history(row) -> dict[str, Any]:
    return dict(zip(_HISTORY_COLUMN_NAMES, row, strict=True))


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
# --------------------------------------------------------------------------


class WarehouseRunStore:
    """Delta via the Statement Execution API.

    Correct enough to run on, and honest about what it cannot do: the ceiling
    check here is count-then-insert with no transaction around it, so two
    simultaneous triggers can both pass. Postgres is the fix; this exists so
    the platform works before Lakebase is provisioned.
    """

    name = "warehouse"

    def __init__(self, repo: Any) -> None:
        self.repo = repo

    async def ensure_schema(self) -> None:
        return None  # uc_ddl/ owns this table

    async def claim_slot(
        self, run_id: str, *, model: str, ceiling: int, requested_by: str | None = None
    ) -> RunRecord:
        active = await self.repo.active_run_count()
        if active >= ceiling:
            raise SlotDenied(active, ceiling)
        if await self.repo.run_status(run_id) is not None:
            raise DuplicateRun(f"run_id {run_id!r} is already registered")

        await self.repo.create_run(run_id, model=model, job_run_id=None, requested_by=requested_by)
        now = now_ms()
        return RunRecord(
            run_id=run_id,
            model=model,
            status=RunStatus.QUEUED,
            started_ts=now,
            updated_ts=now,
            requested_by=requested_by,
        )

    async def attach_job_run(self, run_id: str, job_run_id: str | int) -> None:
        await self.repo.set_run_status(run_id, RunStatus.QUEUED.value, job_run_id=str(job_run_id))

    async def release_slot(self, run_id: str) -> None:
        # Delta has no cheap single-row delete on this path, and a stranded
        # QUEUED row is corrected by startup reconciliation. Saying so beats
        # a delete that pretends to be transactional.
        log.info("warehouse store cannot release %s; reconciliation will correct it", run_id)

    async def set_status(
        self, run_id: str, status: RunStatus | str, *, detail: str | None = None
    ) -> None:
        value = status.value if isinstance(status, RunStatus) else str(status)
        await self.repo.set_run_status(run_id, value, detail=detail)

    async def get(self, run_id: str) -> RunRecord | None:
        row = await self.repo.run_status(run_id)
        return RunRecord.from_row(row) if row else None

    async def list_runs(
        self, *, limit: int = 50, status: str | None = None, model: str | None = None
    ) -> list[RunRecord]:
        rows = await self.repo.list_runs(limit=limit, status=status, model=model)
        return [RunRecord.from_row(r) for r in rows]

    async def active_count(self) -> int:
        return await self.repo.active_run_count()

    async def non_terminal(self, limit: int = 200) -> list[RunRecord]:
        return [RunRecord.from_row(r) for r in await self.repo.non_terminal_runs(limit)]

    async def close(self) -> None:
        return None
