"""Finding a Spark session, including from a thread that did not make one.

`SparkWriter` asked `SparkSession.getActiveSession()` and gave up if that was
None. `getActiveSession()` is **thread-local**, and since the harness moved
onto a worker thread — which is what lets it run inside the ipykernel a
serverless task execs it in — a real Databricks runtime with a perfectly good
session reported:

    no durable writer available: there is no active Spark session

pyspark is deliberately absent from this project's dependency set, so these
tests install fakes into `sys.modules` rather than importing it. That is also
what the real code relies on locally: every route raises ImportError and the
caller falls through to JSONL.
"""

from __future__ import annotations

import sys
import threading
import types

import pytest

from job.delta import JsonlWriter, SparkWriter, select_writer


class FakeSession:
    def __init__(self, label: str) -> None:
        self.label = label


def fake_pyspark(*, active: FakeSession | None, default: FakeSession | None) -> types.ModuleType:
    """A `pyspark.sql` whose two lookups can disagree — which is the whole
    point: on a worker thread the active one is None and the default is not."""

    class SparkSession:
        @staticmethod
        def getActiveSession():
            return active

        class builder:  # noqa: N801 - mirrors pyspark's own naming
            @staticmethod
            def getOrCreate():
                if default is None:
                    raise RuntimeError("could not build a session")
                return default

    module = types.ModuleType("pyspark.sql")
    module.SparkSession = SparkSession
    return module


@pytest.fixture
def spark_modules(monkeypatch):
    """Install/remove fake `pyspark.sql` and `databricks.connect`."""

    def _install(*, active=None, default=None, connect=None):
        for name in ("pyspark", "pyspark.sql", "databricks", "databricks.connect"):
            monkeypatch.delitem(sys.modules, name, raising=False)

        pyspark = types.ModuleType("pyspark")
        sql = fake_pyspark(active=active, default=default)
        pyspark.sql = sql
        monkeypatch.setitem(sys.modules, "pyspark", pyspark)
        monkeypatch.setitem(sys.modules, "pyspark.sql", sql)

        if connect is not None:
            databricks = types.ModuleType("databricks")
            mod = types.ModuleType("databricks.connect")

            class DatabricksSession:
                class builder:  # noqa: N801
                    @staticmethod
                    def getOrCreate():
                        return connect

            mod.DatabricksSession = DatabricksSession
            databricks.connect = mod
            monkeypatch.setitem(sys.modules, "databricks", databricks)
            monkeypatch.setitem(sys.modules, "databricks.connect", mod)

    return _install


class TestFindingASession:
    def test_the_active_session_is_used_when_there_is_one(self, spark_modules):
        spark_modules(active=FakeSession("active"), default=FakeSession("default"))
        assert SparkWriter()._spark.label == "active"

    def test_the_default_session_is_found_when_this_thread_has_no_active_one(
        self, spark_modules
    ):
        """The bug, exactly. `getActiveSession()` is thread-local; the default
        session is process-wide and is what the kernel already built."""
        spark_modules(active=None, default=FakeSession("default"))
        assert SparkWriter()._spark.label == "default"

    def test_databricks_connect_is_preferred_over_plain_pyspark(self, spark_modules):
        """On serverless the session is a Spark Connect client, and
        databricks-connect is what knows how to make one."""
        spark_modules(active=None, default=FakeSession("default"), connect=FakeSession("connect"))
        assert SparkWriter()._spark.label == "connect"

    def test_a_route_that_raises_does_not_stop_the_next_one(self, spark_modules):
        """databricks-connect is absent on a classic runtime, and importing it
        there must fall through rather than fail the run."""
        spark_modules(active=None, default=FakeSession("default"), connect=None)
        assert SparkWriter()._spark.label == "default"

    def test_no_session_anywhere_is_an_error_naming_what_it_tried(self, spark_modules):
        spark_modules(active=None, default=None)
        with pytest.raises(RuntimeError, match="none could be created"):
            SparkWriter()

    def test_an_explicitly_supplied_session_skips_the_search(self, spark_modules):
        spark_modules(active=FakeSession("active"), default=None)
        assert SparkWriter(FakeSession("handed in"))._spark.label == "handed in"


class TestFromAWorkerThread:
    """Where the harness actually runs on serverless, per `job/main.py::_run`."""

    def test_a_worker_thread_still_gets_a_writer(self, spark_modules, monkeypatch):
        # A real active session belongs to the MAIN thread. Model that by
        # answering None for `getActiveSession` — which is what a worker thread
        # observes — while the default session is still there.
        spark_modules(active=None, default=FakeSession("default"))

        found: list = []
        thread = threading.Thread(target=lambda: found.append(select_writer("auto")))
        thread.start()
        thread.join()

        assert found[0].name == "spark"
        assert found[0]._spark.label == "default"


class TestWithoutPyspark:
    """Locally, where pyspark is deliberately not installed at all."""

    def test_auto_falls_through_to_jsonl_when_it_is_allowed(self, monkeypatch, tmp_path):
        for name in ("pyspark", "pyspark.sql", "databricks.connect"):
            monkeypatch.setitem(sys.modules, name, None)  # import raises ImportError
        monkeypatch.setenv("DBX_ALLOW_LOCAL_WRITER", "1")

        assert isinstance(select_writer("auto", local_root=str(tmp_path)), JsonlWriter)

    def test_auto_refuses_rather_than_writing_locally_by_default(self, monkeypatch, tmp_path):
        """A deployed run quietly writing its telemetry to a container that is
        about to disappear is the worst failure this codebase can have."""
        for name in ("pyspark", "pyspark.sql", "databricks.connect"):
            monkeypatch.setitem(sys.modules, name, None)
        monkeypatch.delenv("DBX_ALLOW_LOCAL_WRITER", raising=False)

        with pytest.raises(RuntimeError, match="no durable writer available"):
            select_writer("auto", local_root=str(tmp_path))


class Field:
    def __init__(self, name):
        self.name = name


class Schema:
    def __init__(self, *names):
        self.fields = [Field(n) for n in names]


class FakeTable:
    def __init__(self, schema):
        self.schema = schema


class RecordingSpark:
    """Enough Spark to see exactly what write_batch builds."""

    def __init__(self, schemas):
        self._schemas = schemas
        self.created: list[tuple] = []
        self.saved: list[str] = []
        self.table_lookups: list[str] = []

    def table(self, name):
        self.table_lookups.append(name)
        if name not in self._schemas:
            raise RuntimeError(f"[TABLE_OR_VIEW_NOT_FOUND] {name}")
        return FakeTable(self._schemas[name])

    def createDataFrame(self, data, schema=None):  # noqa: N802 - Spark's name
        self.created.append((data, schema))
        return self

    @property
    def write(self):
        return self

    def mode(self, _how):
        return self

    def saveAsTable(self, name):  # noqa: N802 - Spark's name
        self.saved.append(name)


PROGRESS = Schema(
    "run_id", "seq", "ts", "elapsed_seconds", "percent_complete",
    "primary_metric", "primary_metric_label", "payload_json",
)


class TestWritingAgainstTheTablesOwnSchema:
    """`createDataFrame(rows)` infers from the batch alone, and a column that
    is null in every row of a batch has no type to infer:

        [CANNOT_DETERMINE_TYPE] Some of types cannot be determined after
        inferring.

    Not an edge case: `run_progress.percent_complete` is null for the whole of
    a MILP run, so the first flush of a Gurobi run failed and took the results
    table with it.
    """

    def test_a_batch_whose_column_is_null_throughout_still_writes(self):
        spark = RecordingSpark({"main.dbx_leaning.run_progress": PROGRESS})
        rows = [
            {
                "run_id": "r1", "seq": i, "ts": 1, "elapsed_seconds": 0.5,
                "percent_complete": None,        # a MILP cannot know this
                "primary_metric": 129.5,
                "primary_metric_label": None,    # nor did this model set one
                "payload_json": "{}",
            }
            for i in range(3)
        ]

        written = SparkWriter(spark).write_batch("main.dbx_leaning.run_progress", rows)

        assert written == 3
        data, schema = spark.created[0]
        assert schema is PROGRESS, "the table's schema, not inference"
        assert spark.saved == ["main.dbx_leaning.run_progress"]

    def test_rows_are_ordered_to_match_the_schema(self):
        """A dict has no order. Passing tuples in field order removes any
        question of which value lands in which column."""
        spark = RecordingSpark({"t": Schema("a", "b", "c")})
        SparkWriter(spark).write_batch("t", [{"c": 3, "a": 1, "b": 2}])

        data, _ = spark.created[0]
        assert data == [(1, 2, 3)]

    def test_a_column_the_row_omits_becomes_null(self):
        spark = RecordingSpark({"t": Schema("a", "b")})
        SparkWriter(spark).write_batch("t", [{"a": 1}])

        data, _ = spark.created[0]
        assert data == [(1, None)]

    def test_a_key_the_table_does_not_have_is_refused_by_name(self):
        """Silently dropping it would lose data while reporting success."""
        spark = RecordingSpark({"t": Schema("a")})
        with pytest.raises(ValueError, match=r"no column\(s\) \['typo'\]"):
            SparkWriter(spark).write_batch("t", [{"a": 1, "typo": 2}])

    def test_the_schema_is_read_once_per_table(self):
        spark = RecordingSpark({"t": Schema("a")})
        writer = SparkWriter(spark)
        for _ in range(4):
            writer.write_batch("t", [{"a": 1}])

        assert spark.table_lookups == ["t"], "a metadata round trip per flush is waste"

    def test_an_empty_batch_touches_nothing(self):
        spark = RecordingSpark({"t": Schema("a")})
        assert SparkWriter(spark).write_batch("t", []) == 0
        assert spark.table_lookups == [] and spark.created == []

    def test_a_missing_table_fails_loudly(self):
        """Rather than being inferred into existence with the wrong types."""
        spark = RecordingSpark({})
        with pytest.raises(RuntimeError, match="TABLE_OR_VIEW_NOT_FOUND"):
            SparkWriter(spark).write_batch("main.dbx_leaning.nope", [{"a": 1}])
