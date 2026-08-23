"""The job entrypoint. Thin, but it is the only thing standing between a job
parameter and the harness reading it."""

from __future__ import annotations

import pytest

from entrypoints.run_model import ROOT, parse_settings


def test_key_value_arguments_become_settings():
    assert parse_settings(["DBX_RUN_ID=abc", "DBX_MODEL=models.scenario"]) == {
        "DBX_RUN_ID": "abc",
        "DBX_MODEL": "models.scenario",
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
    assert (ROOT / "pyproject.toml").exists()
    assert (ROOT / "job" / "main.py").exists()
