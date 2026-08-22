"""``write_batch(table, rows)`` — one interface, chosen once at startup.

delta-rs preferred, Spark the real second implementation (not an emergency
path: on Databricks serverless a Spark session already exists, so its cost is
paid once per run, not per flush). Nothing outside this module branches on
which one is in use.

Cloud caveat carried over from ``docs/free-edition-constraints.md``: delta-rs
writing to **S3** needs a locking provider for safe concurrent writers.
Concurrent blind appends cannot conflict at the Delta protocol level, so this
only bites on S3 — check the workspace's cloud before relying on several jobs
appending to one table through delta-rs.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger(__name__)

__all__ = ["BatchWriter", "DeltaRsWriter", "SparkWriter", "JsonlWriter", "select_writer"]


@runtime_checkable
class BatchWriter(Protocol):
    """The whole durable-write surface. Blocking — callers run it off-loop."""

    name: str

    def write_batch(self, table: str, rows: list[dict[str, Any]]) -> int:
        """Append ``rows`` to ``table``. Returns rows written. Raises on failure."""

    def close(self) -> None: ...


class DeltaRsWriter:
    """Preferred. Writes straight to the table's storage location."""

    name = "delta-rs"

    def __init__(self, *, storage_options: dict[str, str] | None = None) -> None:
        from deltalake import write_deltalake  # noqa: F401  (import = availability check)

        self._storage_options = storage_options or {}
        self._lock = threading.Lock()

    def write_batch(self, table: str, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        import pyarrow as pa
        from deltalake import write_deltalake

        batch = pa.Table.from_pylist(rows)
        with self._lock:
            write_deltalake(
                table,
                batch,
                mode="append",
                storage_options=self._storage_options or None,
                schema_mode="merge",
            )
        return len(rows)

    def close(self) -> None:  # nothing to release
        return None


class SparkWriter:
    """Fallback, and a legitimate one. Uses the session the job already has."""

    name = "spark"

    def __init__(self, spark: Any | None = None) -> None:
        if spark is None:
            spark = self._active_session()
        if spark is None:
            raise RuntimeError("no active Spark session")
        self._spark = spark
        self._lock = threading.Lock()

    @staticmethod
    def _active_session() -> Any | None:
        try:
            from pyspark.sql import SparkSession
        except ImportError:
            return None
        return SparkSession.getActiveSession()

    def write_batch(self, table: str, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        with self._lock:
            df = self._spark.createDataFrame(rows)
            df.write.mode("append").saveAsTable(table)
        return len(rows)

    def close(self) -> None:
        return None


class JsonlWriter:
    """Local newline-delimited JSON. Development and tests only.

    Deliberately present so the whole harness can be exercised end to end
    with no Databricks connection — which is what makes "the app is down and
    the job runs anyway" a testable property rather than an aspiration.
    """

    name = "jsonl"

    def __init__(self, root: str | Path = ".delta-local") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write_batch(self, table: str, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        path = self.root / f"{table.replace('/', '_')}.jsonl"
        with self._lock:
            with path.open("a", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row, default=str) + "\n")
        return len(rows)

    def read_all(self, table: str) -> list[dict[str, Any]]:
        path = self.root / f"{table.replace('/', '_')}.jsonl"
        if not path.exists():
            return []
        with path.open(encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def close(self) -> None:
        return None


def select_writer(kind: str = "auto", *, local_root: str = ".delta-local") -> BatchWriter:
    """Pick the implementation once, at process start.

    ``auto`` prefers delta-rs, falls back to Spark, and only then to local
    JSONL — which is a development convenience and says so loudly, because
    silently writing a production run's telemetry to a local file that the
    container throws away would be worse than failing.
    """
    kind = (kind or "auto").lower()

    if kind == "delta-rs":
        return DeltaRsWriter()
    if kind == "spark":
        return SparkWriter()
    if kind == "jsonl":
        return JsonlWriter(local_root)
    if kind != "auto":
        raise ValueError(f"unknown writer {kind!r}; expected auto|delta-rs|spark|jsonl")

    try:
        writer = DeltaRsWriter()
        log.info("durable writer: delta-rs")
        return writer
    except Exception as exc:  # noqa: BLE001 - any import/runtime problem means "not available"
        log.info("delta-rs unavailable (%s), trying Spark", exc)

    try:
        writer = SparkWriter()
        log.info("durable writer: spark")
        return writer
    except Exception as exc:  # noqa: BLE001
        log.info("Spark unavailable (%s)", exc)

    if os.environ.get("DBX_ALLOW_LOCAL_WRITER", "").lower() in {"1", "true", "yes"}:
        log.warning(
            "durable writer: LOCAL JSONL at %s — telemetry is NOT going to Unity Catalog",
            local_root,
        )
        return JsonlWriter(local_root)

    raise RuntimeError(
        "no durable writer available: delta-rs is not importable and there is no "
        "active Spark session. Install the [delta] extra, run on a cluster with "
        "Spark, or set DBX_ALLOW_LOCAL_WRITER=1 to write local JSONL for development."
    )
