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
