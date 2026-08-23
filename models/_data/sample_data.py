"""Reading Databricks' free `samples` catalog, with a deterministic fallback.

Free Edition ships sample data — the `samples` catalog — so the models can run
against something real instead of a sine wave. But every model in this repo
must still run standalone, with no workspace, because that is how its tests
run and how a contributor works. So every loader here has the same shape:

    try the real table -> fall back to a deterministic generator -> say which

**Spark, not the SQL warehouse.** A serverless job already has a Spark session,
so reading a UC table there costs nothing extra. Going through the warehouse
would wake it up for the duration of the read, which is the cost mistake this
whole rewrite exists to avoid (docs/architecture.md).

**Provenance is part of the result.** A model that silently fell back to
synthetic data, and a model that read a year of real trips, must not look
identical afterwards — every loader returns a `Dataset` that says which
happened, and models are expected to log it and carry it into their results.

This module lives under `models/` and is imported by models only. It is not
part of the platform: it imports nothing from `app/`, `job/` or `shared/`, and
a model using it stays independently deployable.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "Dataset",
    "SAMPLES_CATALOG",
    "spark_session",
    "samples_available",
    "query",
    "load",
]

#: Databricks' read-only sample data. Present on Free Edition; a workspace can
#: still have it disabled, which is why nothing here assumes it.
SAMPLES_CATALOG = "samples"


@dataclass(frozen=True)
class Dataset:
    """Rows plus where they came from."""

    rows: list[dict[str, Any]]
    #: The table this came from, or "synthetic".
    source: str
    synthetic: bool
    #: Why the real table was not used, when it was not. None on success.
    reason: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.rows)

    def column(self, name: str) -> list[Any]:
        return [row[name] for row in self.rows]

    def floats(self, name: str, *, default: float | None = None) -> list[float]:
        """Numeric column.

        A real aggregate can be NULL — ``AVG(fare_amount)`` over an hour whose
        fares are all null returns nothing — and ``float(None)`` raises a bare
        TypeError from deep inside a model. That failure only ever happens on
        a workspace, never in the offline suite, which is the worst shape a
        bug can have. So: either substitute an explicit ``default``, or get an
        error that names the column and the row.

        To drop the offending rows instead, and keep every column aligned
        while doing it, use :meth:`dropna`.
        """
        out: list[float] = []
        for index, row in enumerate(self.rows):
            value = row.get(name)
            if value is None or (isinstance(value, float) and not math.isfinite(value)):
                if default is None:
                    raise ValueError(
                        f"{self.source} row {index}: column {name!r} is {value!r}. "
                        f"Pass a default, or drop the row with .dropna({name!r})."
                    )
                out.append(default)
                continue
            out.append(float(value))
        return out

    def dropna(self, *columns: str) -> Dataset:
        """Rows where every named column is present and finite.

        Returns a new Dataset so provenance travels with the filtered rows —
        including a row count that reflects what a model actually used, not
        what the table happened to contain.
        """
        wanted = columns or tuple(self.rows[0]) if self.rows else ()

        def usable(row: dict[str, Any]) -> bool:
            for name in wanted:
                value = row.get(name)
                if value is None:
                    return False
                if isinstance(value, float) and not math.isfinite(value):
                    return False
            return True

        kept = [row for row in self.rows if usable(row)]
        dropped = len(self.rows) - len(kept)
        meta = dict(self.meta)
        if dropped:
            meta["rows_dropped"] = meta.get("rows_dropped", 0) + dropped
        return Dataset(
            rows=kept,
            source=self.source,
            synthetic=self.synthetic,
            reason=self.reason,
            meta=meta,
        )

    @property
    def provenance(self) -> str:
        """One line a model can put straight into a log message."""
        if self.synthetic:
            return f"synthetic data ({self.source}): {self.reason or 'no reason given'}"
        return f"{len(self.rows)} rows from {self.source}"

    def describe(self) -> dict[str, Any]:
        """Provenance as fields, for a result row or an envelope payload.

        Always the same keys, including ``data_fallback_reason: None`` on a
        successful read. An earlier version omitted it on success, which gave
        one results table two different row schemas depending on how the run
        went — and every model track independently wrote the same workaround
        for it, which was the signal that the omission was the bug.
        """
        return {
            "data_source": self.source,
            "data_synthetic": self.synthetic,
            "data_rows": len(self.rows),
            "data_fallback_reason": self.reason,
        }


def spark_session() -> Any | None:
    """The job's existing Spark session, or None when running off-platform."""
    try:
        from pyspark.sql import SparkSession
    except ImportError:
        return None
    try:
        return SparkSession.getActiveSession() or SparkSession.builder.getOrCreate()
    except Exception:  # noqa: BLE001 - no session is a normal local state
        return None


def samples_available(spark: Any | None = None) -> bool:
    spark = spark or spark_session()
    if spark is None:
        return False
    try:
        spark.sql(f"DESCRIBE CATALOG {SAMPLES_CATALOG}").collect()
    except Exception:  # noqa: BLE001
        return False
    return True


def query(sql: str, *, limit: int | None = None) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Run ``sql`` through Spark. Returns ``(rows, reason_it_failed)``.

    Never raises: a model must not fail because sample data was unavailable.
    """
    spark = spark_session()
    if spark is None:
        return None, "no Spark session (not running on a Databricks cluster)"
    try:
        frame = spark.sql(sql)
        if limit is not None:
            frame = frame.limit(limit)
        return [row.asDict(recursive=True) for row in frame.collect()], None
    except Exception as exc:  # noqa: BLE001
        reason = f"{type(exc).__name__}: {exc}"
        log.info("sample-data query failed, falling back to synthetic (%s)", reason)
        return None, reason


def load(
    sql: str,
    *,
    source: str,
    fallback: Callable[[], Sequence[dict[str, Any]]],
    fallback_name: str = "synthetic",
    limit: int | None = None,
    minimum_rows: int = 1,
) -> Dataset:
    """The one entry point: real rows if they are there, generated ones if not.

    ``minimum_rows`` guards the case that actually bites — a table that exists
    but comes back nearly empty, which would otherwise produce a model that
    "ran fine" on four rows.
    """
    rows, reason = query(sql, limit=limit)

    if rows is not None and len(rows) < minimum_rows:
        reason = f"{source} returned {len(rows)} rows, need at least {minimum_rows}"
        log.info("%s; falling back to synthetic", reason)
        rows = None

    if rows is None:
        return Dataset(
            rows=list(fallback()),
            source=fallback_name,
            synthetic=True,
            reason=reason,
        )
    return Dataset(rows=rows, source=source, synthetic=False)
