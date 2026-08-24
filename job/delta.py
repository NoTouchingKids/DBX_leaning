"""``write_batch(table, rows)`` — one interface, chosen once at startup.

**Spark is the implementation. delta-rs is the target, and is not built.**

That is a reversal of the original design, forced by how delta-rs actually
behaves. ``write_deltalake()`` takes a *path or URI*, not a Unity Catalog
name — and handed ``"main.dbx_leaning.run_logs"`` it does not raise. It
creates a local directory with that literal name and writes there. On a
deployed job that means every log, progress point and result lands in the
container's ephemeral filesystem and disappears with it, while the run
reports SUCCEEDED with an accurate-looking ``row_count``.

That is the worst failure this codebase can have: it defeats the "SUCCEEDED
is impossible over a lost write" rule in ``job/runner.py``, because from the
writer's point of view nothing failed. So ``DeltaRsWriter`` now refuses to
run rather than doing that quietly.

Making it real needs a storage URI plus Unity Catalog credential vending —
see ``docs/free-edition-constraints.md``. Until then Spark is not a fallback,
it is the write path, and it is a legitimate one: on serverless a session
already exists, so the cost is paid once per run rather than per flush.

Cloud caveat for whoever does build it: delta-rs writing to **S3** needs a
locking provider for safe concurrent writers. Concurrent blind appends cannot
conflict at the Delta protocol level, so this only bites on S3.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger(__name__)

__all__ = [
    "BatchWriter",
    "DeltaRsWriter",
    "SparkWriter",
    "JsonlWriter",
    "select_writer",
    "DELTA_RS_UNIMPLEMENTED",
]


@runtime_checkable
class BatchWriter(Protocol):
    """The whole durable-write surface. Blocking — callers run it off-loop."""

    name: str

    def write_batch(self, table: str, rows: list[dict[str, Any]]) -> int:
        """Append ``rows`` to ``table``. Returns rows written. Raises on failure."""

    def close(self) -> None: ...


#: What a caller has to supply before delta-rs can be built for real.
DELTA_RS_UNIMPLEMENTED = (
    "the delta-rs writer is not implemented. write_deltalake() takes a storage "
    "URI, not a Unity Catalog name — given a three-part name it silently writes "
    "to a local directory of that name, so a deployed run would report SUCCEEDED "
    "while its telemetry went nowhere. Building it needs a table location plus UC "
    "credential vending. Use the Spark writer (DBX_WRITER=spark, or auto)."
)


class DeltaRsWriter:
    """The intended implementation. Deliberately not built — see the module
    docstring, and ``DELTA_RS_UNIMPLEMENTED`` for what it would need.

    Kept as a named class rather than deleted so the interface it is meant to
    satisfy stays visible, and so ``DBX_WRITER=delta-rs`` fails with a reason
    instead of an unknown-writer error.
    """

    name = "delta-rs"

    def __init__(self, *, storage_options: dict[str, str] | None = None) -> None:
        raise NotImplementedError(DELTA_RS_UNIMPLEMENTED)

    def write_batch(self, table: str, rows: list[dict[str, Any]]) -> int:
        raise NotImplementedError(DELTA_RS_UNIMPLEMENTED)

    def close(self) -> None:
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

    ``auto`` means Spark, then local JSONL — which is a development
    convenience and says so loudly, because silently writing a production
    run's telemetry to a local file the container throws away would be worse
    than failing. delta-rs is skipped: it is not implemented, and picking it
    automatically is exactly how the silent-local-write bug would return.
    """
    kind = (kind or "auto").lower()

    if kind == "delta-rs":
        return DeltaRsWriter()  # raises NotImplementedError, with the reason
    if kind == "spark":
        return SparkWriter()
    if kind == "jsonl":
        return JsonlWriter(local_root)
    if kind != "auto":
        raise ValueError(f"unknown writer {kind!r}; expected auto|delta-rs|spark|jsonl")

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
        "no durable writer available: there is no active Spark session, and "
        "delta-rs is not implemented (see DELTA_RS_UNIMPLEMENTED). Run on a "
        "cluster with Spark, or set DBX_ALLOW_LOCAL_WRITER=1 to write local "
        "JSONL for development."
    )
