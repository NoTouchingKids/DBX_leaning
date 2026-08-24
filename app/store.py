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
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from shared.envelope import TERMINAL_STATUSES, RunStatus, now_ms

log = logging.getLogger(__name__)

__all__ = [
    "RunRecord",
    "RunStore",
    "PostgresRunStore",
    "WarehouseRunStore",
    "SlotDenied",
    "DuplicateRun",
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

SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS run_status (
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
    ON run_status (updated_ts DESC)
    WHERE status NOT IN ({TERMINAL_SQL_LIST});

CREATE INDEX IF NOT EXISTS run_status_recent_idx ON run_status (updated_ts DESC);
"""

#: One well-known lock id, so every ceiling check serialises against the
#: others. Arbitrary but fixed; changing it would let two app versions race.
_CEILING_LOCK_ID = 230825001


class PostgresRunStore:
    """Lakebase. Standard Postgres — nothing here is Databricks-specific
    except how the password is obtained."""

    name = "postgres"

    def __init__(self, dsn: str, *, connect=None) -> None:
        self._dsn = dsn
        self._connect = connect  # injectable for tests

    async def _conn(self):
        if self._connect is not None:
            return await self._connect()
        import psycopg

        return await psycopg.AsyncConnection.connect(self._dsn, autocommit=True)

    async def ensure_schema(self) -> None:
        conn = await self._conn()
        try:
            await conn.execute(SCHEMA_SQL)
        finally:
            await conn.close()

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
                    f"SELECT COUNT(*) FROM run_status WHERE status NOT IN ({TERMINAL_SQL_LIST})"
                )
                active = int((await cur.fetchone())[0])
                if active >= ceiling:
                    await conn.rollback()
                    raise SlotDenied(active, ceiling)

                await cur.execute(
                    """
                    INSERT INTO run_status
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
                "UPDATE run_status SET job_run_id = %s, updated_ts = %s WHERE run_id = %s",
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
                "DELETE FROM run_status WHERE run_id = %s AND status = %s",
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
                """
                INSERT INTO run_status
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
                await cur.execute(f"SELECT {_COLUMNS} FROM run_status WHERE run_id = %s", (run_id,))
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
                    f"SELECT {_COLUMNS} FROM run_status {where} "
                    f"ORDER BY updated_ts DESC LIMIT %s",
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
                    f"SELECT COUNT(*) FROM run_status WHERE status NOT IN ({TERMINAL_SQL_LIST})"
                )
                return int((await cur.fetchone())[0])
        finally:
            await conn.close()

    async def non_terminal(self, limit: int = 200) -> list[RunRecord]:
        conn = await self._conn()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT {_COLUMNS} FROM run_status "
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

        await self.repo.create_run(
            run_id, model=model, job_run_id=None, requested_by=requested_by
        )
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
        await self.repo.set_run_status(
            run_id, RunStatus.QUEUED.value, job_run_id=str(job_run_id)
        )

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
