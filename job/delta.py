"""``write_batch(table, rows)`` — one interface, chosen once at startup.

Spark is the implementation, and on serverless that costs nothing extra: a
session already exists, so it is paid for once per run rather than per flush.

**delta-rs used to be the intended target and is gone.** Not deferred —
removed, because the reason it could never be selected was not a missing
feature. ``write_deltalake()`` takes a *path or URI*, not a Unity Catalog
name, and handed ``"main.dbx_leaning.run_logs"`` it does not raise: it
creates a local directory with that literal name and writes there. On a
deployed job that means every log, progress point and result lands in the
container's ephemeral filesystem and disappears with it, while the run
reports SUCCEEDED with an accurate-looking ``row_count``. Keeping a class
that could only ever raise was carrying the shape of that mistake around;
building it for real needs credential vending and a table location, and that
is a new piece of work rather than a switch to flip.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger(__name__)

__all__ = [
    "BatchWriter",
    "SparkWriter",
    "JsonlWriter",
    "select_writer",
    "WriterKind",
]


@runtime_checkable
class BatchWriter(Protocol):
    """The whole durable-write surface. Blocking — callers run it off-loop."""

    name: str

    def write_batch(self, table: str, rows: list[dict[str, Any]]) -> int:
        """Append ``rows`` to ``table``. Returns rows written. Raises on failure."""

    def close(self) -> None: ...


class SparkWriter:
    """The write path. Uses the session the job already has, or asks for one."""

    name = "spark"

    def __init__(self, spark: Any | None = None) -> None:
        if spark is None:
            spark = self._session()
        if spark is None:
            raise RuntimeError("no Spark session, and none could be created")
        self._spark = spark
        self._lock = threading.Lock()
        #: table -> its Delta schema, read once. A metadata round trip per
        #: flush would be wasteful and the schema does not change under us.
        self._schemas: dict[str, Any] = {}

    @staticmethod
    def _session() -> Any | None:
        """Get a session, creating or attaching to one if there is none here.

        Three routes, cheapest first, because `getActiveSession()` alone is not
        enough and the reason is easy to miss:

        **`getActiveSession()` is THREAD-LOCAL.** A serverless task execs the
        entrypoint inside an ipykernel that already runs an event loop, so
        `job/main.py::_run` puts the harness on a worker thread — and a worker
        thread has no active session even when the process plainly has one. It
        looked exactly like a runtime with no Spark at all:

            no durable writer available: there is no active Spark session

        `getOrCreate()` reads the *default* session, which is process-wide, so
        it finds what the kernel already built and creates nothing. Only where
        there is genuinely nothing does it build one.

        `DatabricksSession` comes first of the two because on serverless the
        session is a Spark Connect client, and databricks-connect is the thing
        that knows how to make one. It is absent on a classic runtime, where
        plain pyspark is right.

        None of this can fire by accident off a workspace: pyspark is
        deliberately absent from this project's dependency set (see the note in
        pyproject.toml), so every import below raises ImportError locally and
        the caller falls through to JSONL.
        """
        for describe, build in (
            ("the active session", _active_session),
            ("databricks-connect", _databricks_connect_session),
            ("pyspark's default session", _default_session),
        ):
            try:
                session = build()
            except Exception as exc:  # noqa: BLE001 - each route is optional
                log.info("no Spark from %s: %s", describe, exc)
                continue
            if session is not None:
                log.info("Spark session from %s", describe)
                return session
        return None

    def _schema(self, table: str) -> Any:
        """The target table's real schema, cached.

        Read from Unity Catalog rather than inferred, because inference cannot
        see what a batch does not contain — see `write_batch`.
        """
        if table not in self._schemas:
            self._schemas[table] = self._spark.table(table).schema
        return self._schemas[table]

    def write_batch(self, table: str, rows: list[dict[str, Any]]) -> int:
        """Append `rows` to `table`, against the table's own schema.

        NOT `createDataFrame(rows)`. Inference works from the batch alone, and
        a column that is null in every row of a batch has no type to infer:

            [CANNOT_DETERMINE_TYPE] Some of types cannot be determined after
            inferring.

        That is not an edge case here. `run_progress.percent_complete` is null
        for the whole of a MILP run — genuinely unknowable, and the DDL says so
        — and `primary_metric_label` is null for any model that does not set
        one. So the first flush of a Gurobi run failed, took the results table
        with it, and the harness correctly refused to report SUCCEEDED over a
        lost write. Every model would hit it; this one got there first.

        The table already exists with an authoritative schema, so use it. Rows
        become tuples in the schema's field order, which also means a key the
        table does not have fails here, named, rather than being silently
        dropped by a dict-to-column match.
        """
        if not rows:
            return 0
        with self._lock:
            schema = self._schema(table)
            names = [field.name for field in schema.fields]
            known = set(names)
            for row in rows:
                unexpected = set(row) - known
                if unexpected:
                    raise ValueError(
                        f"{table} has no column(s) {sorted(unexpected)}; "
                        f"it has {names}. A row that does not fit the table is a "
                        f"schema mismatch, not something to write around."
                    )
            ordered = [tuple(row.get(name) for name in names) for row in rows]
            df = self._spark.createDataFrame(ordered, schema=schema)
            df.write.mode("append").saveAsTable(table)
        return len(rows)

    def close(self) -> None:
        return None


def _active_session() -> Any | None:
    """Thread-local, and free. Right whenever the caller is on the thread that
    made the session — which the harness is not, on serverless."""
    from pyspark.sql import SparkSession

    return SparkSession.getActiveSession()


def _databricks_connect_session() -> Any | None:
    """Serverless: the session is a Spark Connect client, and this is what
    knows how to build one. Absent on a classic runtime."""
    from databricks.connect import DatabricksSession

    return DatabricksSession.builder.getOrCreate()


def _default_session() -> Any | None:
    """Process-wide rather than thread-local, so it finds a session another
    thread created. Builds one only where there is genuinely none."""
    from pyspark.sql import SparkSession

    return SparkSession.builder.getOrCreate()


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


class WriterKind(StrEnum):
    """The three values ``DBX_WRITER`` accepts.

    An enum rather than bare strings for the same reason the wire protocol
    uses them: the valid set lives in exactly one place. This function
    previously compared against four string literals and then reported the
    valid set from a hand-written message — two lists that had to be kept in
    step by hand, which is the drift this repo has paid for repeatedly.

    It matters more here than the size suggests. This selector chooses the
    DURABLE write path, and getting it wrong is the failure that already
    happened once — see the module docstring on why delta-rs is gone rather
    than merely unimplemented.
    """

    AUTO = "auto"
    SPARK = "spark"
    JSONL = "jsonl"

    @classmethod
    def parse(cls, value: str | None) -> WriterKind:
        """Accept what an env var actually contains: None, case, whitespace."""
        try:
            return cls((value or cls.AUTO.value).strip().lower())
        except ValueError:
            raise ValueError(
                f"unknown writer {value!r}; expected one of {'|'.join(k.value for k in cls)}"
            ) from None


def select_writer(
    kind: str | WriterKind = WriterKind.AUTO, *, local_root: str = ".delta-local"
) -> BatchWriter:
    """Pick the implementation once, at process start.

    ``auto`` means Spark, then local JSONL — which is a development
    convenience and says so loudly, because silently writing a production
    run's telemetry to a local file the container throws away would be worse
    than failing.
    """
    selected = WriterKind.parse(kind if isinstance(kind, str) else kind.value)

    if selected is WriterKind.SPARK:
        return SparkWriter()
    if selected is WriterKind.JSONL:
        return JsonlWriter(local_root)

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
        "no durable writer available: no Spark session could be found or "
        "created (the log above says what each of the three routes reported). "
        "Run somewhere with Spark, or set DBX_ALLOW_LOCAL_WRITER=1 to write "
        "local JSONL for development."
    )
