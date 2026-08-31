"""The heartbeat job definition.

The model-job rules that used to live in `test_bundle.py` went to `dev` with
the models. These are the ones that apply to the single job v4 has, plus one
that is new and is really about a boundary rather than a job.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
JOB_FILE = ROOT / "resources" / "model_heartbeat.job.yml"


@pytest.fixture(scope="module")
def job() -> dict:
    return yaml.safe_load(JOB_FILE.read_text())["resources"]["jobs"]["model_heartbeat"]


@pytest.fixture(scope="module")
def params(job) -> dict[str, str]:
    return {p["name"]: p["default"] for p in job["parameters"]}


def test_it_carries_the_tag_the_app_discovers_it_by(job):
    """Load-bearing, not decorative.

    v4's app finds its jobs by `project: dbx-leaning` rather than being handed
    ids the bundle interpolated — which is what lets a job move to another
    bundle or repo without the app changing. A missing or misspelled tag here
    means the app simply does not see this job.
    """
    assert job["tags"]["project"] == "dbx-leaning"
    assert job["tags"]["model"] == "heartbeat"


def test_it_runs_the_heartbeat_through_the_shared_entrypoint(job, params):
    assert params["DBX_MODEL"] == "job.models.heartbeat"
    task = job["tasks"][0]["spark_python_task"]
    assert task["python_file"].endswith("/job/run_model.py")
    assert task["source"] == "WORKSPACE"


def test_every_declared_parameter_is_forwarded_to_the_entrypoint(job, params):
    """A parameter declared but never passed through is worse than a missing
    one: the run starts and silently ignores its own configuration."""
    task = job["tasks"][0]["spark_python_task"]
    forwarded = {arg.split("=", 1)[0] for arg in task["parameters"]}
    assert forwarded == set(params), f"{set(params) ^ forwarded} not forwarded"

    for arg in task["parameters"]:
        key, _, value = arg.partition("=")
        assert value == f"{{{{job.parameters.{key}}}}}", arg


def test_the_app_never_sends_the_telemetry_volume(params):
    """The grant boundary, showing up in the parameter list.

    The job declares `DBX_TELEMETRY_VOLUME` and supplies its own default; the
    app does not send it, and `JOB_PARAMETER_NAMES` does not contain it. That
    is not an oversight to tidy up later — the app holds no grant on that
    volume (uc_ddl/004_telemetry_volume.sql) and has no business knowing the
    path. Adding it here would be the first symptom of the boundary eroding.
    """
    from server.routes.runs import JOB_PARAMETER_NAMES

    assert "DBX_TELEMETRY_VOLUME" in params, "the job must supply its own telemetry path"
    assert "DBX_TELEMETRY_VOLUME" not in JOB_PARAMETER_NAMES, (
        "the app is sending the telemetry volume; it holds no grant on it and "
        "should not know the path"
    )


def test_the_app_sends_nothing_the_job_has_not_declared(params):
    """Databricks rejects a `run-now` parameter a job has not declared, so
    drift here surfaces as every trigger failing."""
    from server.routes.runs import JOB_PARAMETER_NAMES

    undeclared = set(JOB_PARAMETER_NAMES) - set(params)
    assert not undeclared, f"the app sends {sorted(undeclared)}, which the job does not declare"


def test_it_queues_rather_than_failing_when_the_ceiling_is_hit(job):
    """Free Edition caps concurrent job tasks at 5 account-wide. Queueing is
    how a sixth waits instead of failing."""
    assert job["queue"]["enabled"] is True
    assert job["timeout_seconds"] > 0


def test_it_installs_only_the_harness_floor(job):
    """No model extra, because a heartbeat imports nothing a solver would —
    and the per-model dependency split has nothing to split yet."""
    deps = job["environments"][0]["spec"]["dependencies"]
    assert deps == ["-r ${workspace.file_path}/job/requirements.txt"]
