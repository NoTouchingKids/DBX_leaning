"""The read queries, in one place.

Every one of these is a *client-triggered* read: backfill on demand,
reconciliation once at startup. Nothing here runs on a timer — a periodic
query is what keeps the warehouse awake, and that is the specific cost mistake
this rewrite exists to avoid.
"""

from __future__ import annotations

import json
from typing import Any

from shared.tables import EVENTS, LOGS, PROGRESS, RESULT_META, TableSet

from .sql import P, SqlClient

__all__ = ["RunRepository"]

RUN_STATUS = "run_status"


class RunRepository:
    def __init__(self, sql: SqlClient, tables: TableSet) -> None:
        self.sql = sql
        self.tables = tables

    def t(self, name: str) -> str:
        return self.tables.qualify(name)

    async def messages_since(
        self, run_id: str, after_seq: int, limit: int
    ) -> list[dict[str, Any]]:
        """Backfill: everything a client is missing, in one seq-ordered stream.

        ``after_seq`` is bound as INT, not interpolated and not bound untyped —
        an untyped parameter is compared as a string server-side, so seq 9
        would sort after seq 10 and the cursor would silently stall.
        """
        sql = f"""
            SELECT seq, ts, 'log' AS type,
                   to_json(named_struct(
                       'message', message, 'level', level,
                       'source', source, 'phase', phase,
                       'client_visible', client_visible)) AS body
            FROM {self.t(LOGS)}
            WHERE run_id = :run_id AND seq > :after_seq AND client_visible
            UNION ALL
            SELECT seq, ts, 'progress' AS type,
                   to_json(named_struct(
                       'elapsed_seconds', elapsed_seconds,
                       'percent_complete', percent_complete,
                       'primary_metric', primary_metric,
                       'primary_metric_label', primary_metric_label,
                       'payload_json', payload_json)) AS body
            FROM {self.t(PROGRESS)}
            WHERE run_id = :run_id AND seq > :after_seq
            UNION ALL
            SELECT seq, ts, 'status' AS type,
                   to_json(named_struct('status', status, 'detail', detail)) AS body
            FROM {self.t(EVENTS)}
            WHERE run_id = :run_id AND seq > :after_seq
            UNION ALL
            SELECT seq, ts, 'result' AS type,
                   to_json(named_struct(
                       'row_count', row_count, 'chunk_index', chunk_index,
                       'final', final, 'fetch_hint_json', fetch_hint_json,
                       'preview_json', preview_json)) AS body
            FROM {self.t(RESULT_META)}
            WHERE run_id = :run_id AND seq > :after_seq
            ORDER BY seq
            LIMIT :row_limit
        """
        rows = await self.sql.query(
            sql,
            [P.str("run_id", run_id), P.int("after_seq", after_seq), P.int("row_limit", limit)],
        )
        return [_rehydrate(r) for r in rows]

    async def run_status(self, run_id: str) -> dict[str, Any] | None:
        rows = await self.sql.query(
            f"SELECT * FROM {self.t(RUN_STATUS)} WHERE run_id = :run_id",
            [P.str("run_id", run_id)],
        )
        return rows[0] if rows else None

    async def non_terminal_runs(self, limit: int = 200) -> list[dict[str, Any]]:
        return await self.sql.query(
            f"""
            SELECT run_id, job_run_id, status, updated_ts
            FROM {self.t(RUN_STATUS)}
            WHERE status NOT IN ('SUCCEEDED', 'FAILED', 'CANCELLED', 'INFEASIBLE')
            ORDER BY updated_ts DESC
            LIMIT :row_limit
            """,
            [P.int("row_limit", limit)],
        )

    async def latest_event(self, run_id: str) -> dict[str, Any] | None:
        rows = await self.sql.query(
            f"""
            SELECT status, detail, seq, ts
            FROM {self.t(EVENTS)}
            WHERE run_id = :run_id
            ORDER BY seq DESC
            LIMIT 1
            """,
            [P.str("run_id", run_id)],
        )
        return rows[0] if rows else None

    async def set_run_status(
        self, run_id: str, status: str, *, detail: str | None = None, job_run_id: str | None = None
    ) -> None:
        """The one write this app makes, and it is a single-row UPDATE of
        current state — not telemetry. Bound parameters, no interpolation."""
        await self.sql.query(
            f"""
            MERGE INTO {self.t(RUN_STATUS)} AS t
            USING (SELECT :run_id AS run_id) AS s
            ON t.run_id = s.run_id
            WHEN MATCHED THEN UPDATE SET
                t.status = :status, t.detail = :detail,
                t.updated_ts = :updated_ts
            WHEN NOT MATCHED THEN INSERT (run_id, job_run_id, status, detail, updated_ts)
                VALUES (:run_id, :job_run_id, :status, :detail, :updated_ts)
            """,
            [
                P.str("run_id", run_id),
                P.str("status", status),
                P.str("detail", detail),
                P.str("job_run_id", job_run_id),
                P.bigint("updated_ts", _now_ms()),
            ],
        )


def _now_ms() -> int:
    from shared.envelope import now_ms

    return now_ms()


def _rehydrate(row: dict[str, Any]) -> dict[str, Any]:
    """Flatten the ``body`` JSON back into an envelope-shaped dict."""
    body = row.get("body")
    fields = json.loads(body) if isinstance(body, str) else dict(body or {})

    for packed, unpacked in (
        ("payload_json", "payload"),
        ("fetch_hint_json", "fetch_hint"),
        ("preview_json", "preview"),
    ):
        if packed in fields:
            raw = fields.pop(packed)
            try:
                fields[unpacked] = json.loads(raw) if raw else ({} if unpacked != "preview" else [])
            except (TypeError, ValueError):
                fields[unpacked] = {} if unpacked != "preview" else []

    out = {"type": row["type"], "seq": int(row["seq"]), "ts": int(row["ts"]), **fields}
    for numeric in ("elapsed_seconds", "percent_complete", "primary_metric"):
        if numeric in out and out[numeric] is not None:
            out[numeric] = float(out[numeric])
    for integral in ("row_count", "chunk_index"):
        if integral in out and out[integral] is not None:
            out[integral] = int(out[integral])
    if "final" in out and out["final"] is not None:
        out["final"] = out["final"] in (True, "true", "True", 1, "1")
    if "client_visible" in out and out["client_visible"] is not None:
        out["client_visible"] = out["client_visible"] in (True, "true", "True", 1, "1")
    return out
