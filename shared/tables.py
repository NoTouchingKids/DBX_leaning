"""Where each message lands durably, and what it looks like as a flat row.

Unqualified logical names here; ``TableSet`` qualifies them with the
catalog/schema a deployment actually uses. Nested fields go in as JSON
strings, not VARIANT: VARIANT support in the Python ``deltalake`` bindings
lags the Rust kernel (delta-rs #3637), and CLAUDE.md rates VARIANT
nice-to-have rather than required.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .envelope import (
    LogMessage,
    Message,
    MessageType,
    ProgressMessage,
    ResultMessage,
    StatusMessage,
)

__all__ = ["TableSet", "table_for", "to_row", "LOGS", "PROGRESS", "EVENTS", "RESULT_META"]

LOGS = "run_logs"
PROGRESS = "run_progress"
#: Append-only status transitions written by the *job*. Distinct from
#: ``run_status``, the one-row-per-run current state the *app* maintains: a
#: job that runs while the app is down still records its transitions here,
#: and startup reconciliation reads them back.
EVENTS = "run_events"
#: Result *metadata* — preview, row count, fetch hint. The result rows
#: themselves go to the model's own table.
RESULT_META = "run_results_meta"

_TABLE_BY_TYPE = {
    MessageType.LOG: LOGS,
    MessageType.PROGRESS: PROGRESS,
    MessageType.STATUS: EVENTS,
    MessageType.RESULT: RESULT_META,
}


@dataclass(frozen=True)
class TableSet:
    """Qualifies logical table names for one deployment."""

    catalog: str = "main"
    schema: str = "dbx_leaning"

    def qualify(self, table: str) -> str:
        # Already three-part (a per-model results table may be configured
        # fully qualified) — leave it alone.
        return table if table.count(".") >= 2 else f"{self.catalog}.{self.schema}.{table}"

    def for_message(self, msg: Message) -> str:
        return self.qualify(table_for(msg))


def table_for(msg: Message) -> str:
    return _TABLE_BY_TYPE[MessageType(msg.type)]


def to_row(msg: Message) -> dict[str, Any]:
    """Flat, Delta-writable dict. One shape per destination table."""
    base = {"run_id": msg.run_id, "seq": msg.seq, "ts": msg.ts}

    if isinstance(msg, LogMessage):
        return {
            **base,
            "level": msg.level.value,
            "source": msg.source,
            "phase": msg.phase,
            "message": msg.message,
            # Written regardless — this filters the live send, not the record.
            "client_visible": msg.client_visible,
        }

    if isinstance(msg, ProgressMessage):
        return {
            **base,
            "elapsed_seconds": msg.elapsed_seconds,
            "percent_complete": msg.percent_complete,
            "primary_metric": msg.primary_metric,
            "primary_metric_label": msg.primary_metric_label,
            "payload_json": json.dumps(msg.payload, default=str),
        }

    if isinstance(msg, StatusMessage):
        return {**base, "status": msg.status.value, "detail": msg.detail}

    if isinstance(msg, ResultMessage):
        return {
            **base,
            "chunk_index": msg.chunk_index,
            "final": msg.final,
            "row_count": msg.row_count,
            "fetch_hint_json": json.dumps(msg.fetch_hint, default=str),
            "preview_json": json.dumps(msg.preview, default=str),
        }

    raise TypeError(f"no row projection for {type(msg).__name__}")
