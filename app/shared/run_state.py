"""The one statement that reports a status to Lakebase — shared by both writers.

Two processes write ``run_status``: the app (``server/store.py``, from status
messages arriving over the socket) and the job (``job/lakebase.py``, directly,
per ``docs/v5-implementation-plan.md`` Phase 2). Until Phase 2 item 5 retires
the app-side writer they write the same row, so they must write it the same
way — same seq guard, same history append, same rules for never blanking a
known ``model`` or ``job_run_id``. One text, here, rather than two copies and a
test that they agree.

It lives in ``shared/`` for the reason everything here does: it is the one
package both the app (which deploys ``app/`` alone) and the job (which installs
this repo) can import. The job must not import ``server`` — the job and the app
are two services, and setuptools making it reachable is not a reason to couple
them (see ``REFRESH_SKEW_S`` in ``job/auth.py`` for the same rule).

Stdlib only, like the rest of ``shared/``: this is SQL text and a parameter
dict, not a driver. Each writer brings its own ``psycopg`` — async in the app,
sync in the job.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "DEFAULT_SCHEMA",
    "HISTORY_TABLE",
    "REPORT_SQL",
    "STATUS_TABLE",
    "UnsafeSchemaName",
    "report_params",
    "report_sql",
    "vet_schema",
]

STATUS_TABLE = "run_status"
HISTORY_TABLE = "run_status_history"

#: Where the tables live inside the Lakebase database.
#:
#: NOT `public`, and that is not tidiness. Since PostgreSQL 15 the `public`
#: schema no longer grants CREATE to `PUBLIC`, so a role that is not the
#: database owner gets `permission denied for schema public`. The DDL is
#: applied out of band now, but the same rule decides where it can land.
#:
#: It also mirrors the Unity Catalog side, where everything is in
#: `<catalog>.dbx_leaning` rather than loose in `default`.
DEFAULT_SCHEMA = "dbx_leaning"

#: Postgres identifier, for the one thing here that cannot be a bound
#: parameter. A schema name is an identifier, not a value.
_PG_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class UnsafeSchemaName(ValueError):
    """A schema name that will not be interpolated into SQL."""


def vet_schema(schema: str) -> str:
    """``schema``, if it is a plain Postgres identifier; raises otherwise."""
    if not _PG_IDENTIFIER.match(schema):
        raise UnsafeSchemaName(
            f"{schema!r} is not a plain Postgres identifier; refusing to build SQL from it"
        )
    return schema


#: One status report: the current-state upsert and the history append, in ONE
#: statement.
#:
#: The upsert rides as a data-modifying CTE and the history append is the
#: primary query. Postgres runs a data-modifying CTE exactly once and always to
#: completion whether or not anything reads its output, so `upsert_current` is
#: not dead code despite nothing selecting from it. One statement is one
#: implicit transaction and one round trip.
#:
#: **The seq guard** (`WHERE EXCLUDED.seq >= rs.seq`): a report older than the
#: row does not move it backwards. `seq` is job-assigned, per run, monotonic —
#: the message's own clock. Never `now()`: a late-landing write always carries
#: the later `now()`, which would make the guard inert on exactly the path that
#: needs it. `>=` rather than `>` so an exact redelivery reapplies the same
#: values — idempotent — instead of being a special case.
#:
#: **The history row appends even when the guard makes the upsert a no-op.**
#: The two tables answer different questions: current state is what is true,
#: history is what was reported. `ON CONFLICT (run_id, seq) DO NOTHING` is what
#: keeps a redelivered report one row.
#:
#: **A known `model` or `job_run_id` is never blanked.** `NULLIF(..., '')`
#: before the COALESCE, not a bare COALESCE: `model` is NOT NULL DEFAULT '', so
#: a writer that does not know it sends `''`, and a bare COALESCE would see an
#: empty string rather than a NULL and keep it — the bug `5c57c33` fixed on the
#: prior branch, where a run carried `model=''` for the rest of its life.
#:
#: Every parameter carries an explicit cast. An untyped parameter is how this
#: repo got `"2" > "12"` twice; `seq` here is the guard, and must compare as a
#: number.
#:
#: `{status_table}` and `{history_table}` are schema-qualified names — see
#: :func:`report_sql`, which is the way to fill them in.
REPORT_SQL = """
WITH upsert_current AS (
    INSERT INTO {status_table} AS rs
        (run_id, job_run_id, model, status, terminal, detail, seq, updated_at)
    VALUES (
        %(run_id)s::text,
        NULLIF(%(job_run_id)s::text, ''),
        COALESCE(%(model)s::text, ''),
        %(status)s::text,
        %(terminal)s::boolean,
        %(detail)s::text,
        %(seq)s::bigint,
        %(ts)s::bigint
    )
    ON CONFLICT (run_id) DO UPDATE SET
        job_run_id = COALESCE(EXCLUDED.job_run_id, rs.job_run_id),
        model      = COALESCE(NULLIF(EXCLUDED.model, ''), rs.model),
        status     = EXCLUDED.status,
        terminal   = EXCLUDED.terminal,
        detail     = EXCLUDED.detail,
        seq        = EXCLUDED.seq,
        updated_at = EXCLUDED.updated_at
    WHERE EXCLUDED.seq >= rs.seq
)
INSERT INTO {history_table} (run_id, seq, status, terminal, detail, ts)
VALUES (
    %(run_id)s::text,
    %(seq)s::bigint,
    %(status)s::text,
    %(terminal)s::boolean,
    %(detail)s::text,
    %(ts)s::bigint
)
ON CONFLICT (run_id, seq) DO NOTHING
""".strip()


def report_sql(schema: str = DEFAULT_SCHEMA) -> str:
    """:data:`REPORT_SQL` with both tables qualified by a vetted ``schema``.

    Qualified rather than left to ``search_path``: a search path is
    per-session state, and one that silently reverts to ``public`` finds a
    DIFFERENT, empty table rather than failing.
    """
    vet_schema(schema)
    return REPORT_SQL.format(
        status_table=f"{schema}.{STATUS_TABLE}", history_table=f"{schema}.{HISTORY_TABLE}"
    )


def report_params(
    run_id: str,
    status: str,
    *,
    seq: int,
    terminal: bool,
    ts: int,
    detail: str | None = None,
    model: str | None = "",
    job_run_id: str | None = None,
) -> dict[str, Any]:
    """The parameters :data:`REPORT_SQL` binds, coerced the same way for both
    writers — so the same report produces the same rows whichever sent it.

    ``seq``, ``terminal`` and ``ts`` are the MESSAGE's, never the writing
    process's: the guard compares the message's own clock.
    """
    return {
        "run_id": str(run_id),
        "job_run_id": None if job_run_id is None else str(job_run_id),
        "model": model or "",
        "status": str(status),
        "terminal": bool(terminal),
        "detail": detail,
        "seq": int(seq),
        "ts": int(ts),
    }
