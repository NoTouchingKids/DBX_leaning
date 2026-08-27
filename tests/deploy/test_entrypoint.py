"""The job entrypoint. Thin, but it is the only thing standing between a job
parameter and the harness reading it."""

from __future__ import annotations

import pytest

import job.run_model as run_model
from job.run_model import ROOT_OVERRIDE, parse_settings, repo_root


def test_key_value_arguments_become_settings():
    assert parse_settings(["DBX_RUN_ID=abc", "DBX_MODEL=job.models.scenario"]) == {
        "DBX_RUN_ID": "abc",
        "DBX_MODEL": "job.models.scenario",
    }


def test_an_empty_value_is_dropped_rather_than_exported_as_empty():
    # An unset DBX_APP_URL must mean "no app", not "an app at ''".
    assert parse_settings(["DBX_APP_URL=", "DBX_RUN_ID=x"]) == {"DBX_RUN_ID": "x"}


def test_values_containing_equals_survive_intact():
    # DBX_MODEL_CONFIG is JSON and can contain anything.
    settings = parse_settings(['DBX_MODEL_CONFIG={"a":"b=c"}'])
    assert settings["DBX_MODEL_CONFIG"] == '{"a":"b=c"}'


def test_a_malformed_argument_fails_loudly():
    """A typo'd parameter that silently did nothing would show up as a run
    ignoring its own configuration — miserable to debug from a job log."""
    with pytest.raises(SystemExit, match="expected KEY=VALUE"):
        parse_settings(["DBX_RUN_ID"])
    with pytest.raises(SystemExit, match="empty key"):
        parse_settings(["=value"])


def test_the_root_it_computes_is_the_repo_root():
    root = repo_root()
    assert (root / "pyproject.toml").exists()
    assert (root / "job" / "main.py").exists()


class TestFindingTheRootWithoutDunderFile:
    """A serverless `spark_python_task` does not run this file as a script.

    It reads the file and `exec(compile(source, filename, "exec"))`s it inside
    an ipykernel, so `__file__` is never bound — and the entrypoint's first
    statement used to be `pathlib.Path(__file__)`, which meant every run on a
    real workspace died before parsing a single argument:

        File /Workspace/Users/.../entrypoints/run_model.py:25
        NameError: name '__file__' is not defined

    Nothing offline caught it because `pytest` imports this module normally,
    where `__file__` is exactly what you would expect.
    """

    def test_the_module_body_survives_being_exec_d_the_way_databricks_runs_it(self):
        """The real reproduction, not an approximation of one.

        This is what a serverless task does: read the file, compile it against
        its path, exec it in a bare namespace. The old first statement raised
        NameError here, at import, before `main()` was ever reached.
        """
        path = repo_root() / "job" / "run_model.py"
        # Not `__main__`, so exec'ing it does not also try to run a job.
        namespace: dict = {"__name__": "job_entrypoint"}
        exec(compile(path.read_bytes(), str(path), "exec"), namespace)

        assert "__file__" not in namespace, "the point of the exercise"

        found = namespace["repo_root"]()
        assert found == repo_root()
        assert (found / "job" / "main.py").exists()

    def test_the_path_comes_off_the_code_object(self):
        """`compile(source, filename, ...)` keeps the filename even though the
        namespace has no `__file__` — which is how the traceback could name the
        file and the line while the module could not name itself."""
        path = repo_root() / "job" / "run_model.py"
        namespace: dict = {"__name__": "job_entrypoint"}
        exec(compile(path.read_bytes(), str(path), "exec"), namespace)

        assert dict(namespace["_candidates"]()) == {"code object": repo_root()}

    def test_an_explicit_override_wins(self, tmp_path, monkeypatch):
        tmp_path.joinpath("job").mkdir()
        tmp_path.joinpath("job", "main.py").write_text("")
        monkeypatch.setenv(ROOT_OVERRIDE, str(tmp_path))

        assert repo_root() == tmp_path.resolve()

    def test_a_candidate_without_the_marker_is_rejected_rather_than_used(
        self, tmp_path, monkeypatch
    ):
        """An override pointing somewhere plausible but wrong must not be
        accepted — `sys.path` would gain a directory with no `job/` in it and
        the failure would surface as an ImportError two frames later."""
        monkeypatch.setenv(ROOT_OVERRIDE, str(tmp_path))

        # The real root is still discoverable, so it falls through to that.
        assert repo_root() != tmp_path.resolve()

    def test_no_candidate_at_all_says_what_it_tried(self, monkeypatch):
        monkeypatch.delitem(run_model.__dict__, "__file__")
        monkeypatch.setattr(run_model.sys, "argv", [])
        monkeypatch.setattr(run_model, "_candidates", lambda: [])

        with pytest.raises(SystemExit, match=ROOT_OVERRIDE):
            repo_root()
