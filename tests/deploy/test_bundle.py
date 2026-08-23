"""The bundle is configuration, which means nothing type-checks it and the
usual failure mode is a name that quietly stopped matching.

These tests bind the three things that must agree — the models on disk, the
jobs in the bundle, and the parameters the app sends — so a rename fails here
rather than at 3am on a real workspace.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
RESOURCES = ROOT / "resources"
REQUIREMENTS = ROOT / "deploy" / "requirements"

#: The five model packages, discovered rather than listed.
MODELS = sorted(
    p.name
    for p in (ROOT / "models").iterdir()
    if p.is_dir() and (p / "__init__.py").exists() and not p.name.startswith("_")
)


def load(path: pathlib.Path) -> dict:
    return yaml.safe_load(path.read_text())


@pytest.fixture(scope="module")
def bundle() -> dict:
    return load(ROOT / "databricks.yml")


@pytest.fixture(scope="module")
def jobs() -> dict[str, dict]:
    found = {}
    for path in RESOURCES.glob("*.job.yml"):
        for name, job in load(path)["resources"]["jobs"].items():
            found[name] = job
    return found


def test_there_are_models_to_deploy():
    """Deliberately not an exact count. Free Edition caps *concurrent* job
    tasks at 5, not how many models exist — the app enforces the concurrency
    ceiling and Databricks queues beyond it, so more models than slots is a
    supported state and the thing the microservice split exists to be tested
    against."""
    assert len(MODELS) >= 5, MODELS


def test_every_model_has_its_own_job_file():
    """One job per model, one file per job — the microservice boundary is
    supposed to be visible in the tree, not buried in a shared document."""
    for model in MODELS:
        path = RESOURCES / f"model_{model}.job.yml"
        assert path.exists(), f"{model} has no job file at {path.relative_to(ROOT)}"


def test_no_job_exists_for_a_model_that_does_not(jobs):
    for name in jobs:
        model = name.removeprefix("model_")
        assert model in MODELS, f"job {name} refers to a model that is not in models/"


def test_each_job_runs_its_own_model(jobs):
    for model in MODELS:
        params = {p["name"]: p["default"] for p in jobs[f"model_{model}"]["parameters"]}
        assert params["DBX_MODEL"] == f"models.{model}"


def test_jobs_declare_exactly_the_parameters_the_app_sends(jobs):
    """Databricks rejects a run-now parameter a job has not declared, so this
    drift would surface as every trigger failing."""
    from app.routes.runs import JOB_PARAMETER_NAMES

    for model in MODELS:
        declared = {p["name"] for p in jobs[f"model_{model}"]["parameters"]}
        assert declared == set(JOB_PARAMETER_NAMES), (
            f"model_{model} declares {sorted(declared)}, "
            f"app sends {sorted(JOB_PARAMETER_NAMES)}"
        )


def test_every_declared_parameter_is_forwarded_to_the_entrypoint(jobs):
    """A parameter that is declared but never passed through is worse than a
    missing one: the run starts and silently ignores its own configuration."""
    for model in MODELS:
        job = jobs[f"model_{model}"]
        declared = {p["name"] for p in job["parameters"]}
        task = job["tasks"][0]["spark_python_task"]
        forwarded = {arg.split("=", 1)[0] for arg in task["parameters"]}
        assert forwarded == declared, f"model_{model}: {declared ^ forwarded} not forwarded"

        for arg in task["parameters"]:
            key, _, value = arg.partition("=")
            assert value == f"{{{{job.parameters.{key}}}}}", f"model_{model}: {arg}"


def test_every_job_runs_the_shared_entrypoint(jobs):
    for model in MODELS:
        task = jobs[f"model_{model}"]["tasks"][0]["spark_python_task"]
        assert task["python_file"].endswith("/entrypoints/run_model.py")
        assert task["source"] == "WORKSPACE"  # file sync, not a wheel — for now


def test_each_job_has_its_own_environment_and_requirements(jobs):
    """The whole point of the split: separate environments, separate deps."""
    seen_keys = set()
    for model in MODELS:
        job = jobs[f"model_{model}"]
        env = job["environments"][0]
        assert env["environment_key"] not in seen_keys, "two models share an environment key"
        seen_keys.add(env["environment_key"])
        assert job["tasks"][0]["environment_key"] == env["environment_key"]

        deps = env["spec"]["dependencies"]
        assert deps == [
            f"-r ${{workspace.file_path}}/deploy/requirements/{model}.txt"
        ], f"model_{model} deps: {deps}"
        assert (REQUIREMENTS / f"{model}.txt").exists()


def test_the_fan_out_model_is_the_one_allowed_to_run_concurrently(jobs):
    concurrency = {m: jobs[f"model_{m}"]["max_concurrent_runs"] for m in MODELS}
    assert concurrency["scenario"] > 1, "scenario exists to exercise fan-out"
    assert all(v == 1 for m, v in concurrency.items() if m != "scenario"), concurrency
    # The account-wide ceiling is 5 concurrent TASKS across all models, which
    # no per-job setting can express — the app enforces it before triggering.
    assert concurrency["scenario"] <= 5


def test_every_job_queues_rather_than_failing_when_the_ceiling_is_hit(jobs):
    for model in MODELS:
        assert jobs[f"model_{model}"]["queue"]["enabled"] is True


def test_every_job_has_a_timeout(jobs):
    for model in MODELS:
        assert jobs[f"model_{model}"]["timeout_seconds"] > 0


def test_the_app_knows_about_every_job(bundle):
    """DBX_JOB_IDS is the app's allow-list; a model missing from it cannot be
    triggered no matter how well its job is defined."""
    app = load(RESOURCES / "app.yml")["resources"]["apps"]["dbx_leaning"]
    env = {e["name"]: e for e in app["config"]["env"]}
    job_ids = env["DBX_JOB_IDS"]["value"]

    for model in MODELS:
        assert f'"{model}"' in job_ids, f"{model} missing from DBX_JOB_IDS"
        assert f"${{resources.jobs.model_{model}.id}}" in job_ids, (
            f"{model}'s id is not interpolated from the bundle"
        )


def test_the_app_takes_its_token_from_a_secret_not_a_plain_value(bundle):
    app = load(RESOURCES / "app.yml")["resources"]["apps"]["dbx_leaning"]
    env = {e["name"]: e for e in app["config"]["env"]}

    assert "value" not in env["DBX_APP_TOKEN"], "the ingress token must not be a literal"
    assert env["DBX_APP_TOKEN"]["value_from"] == "app-token"
    secret = next(r for r in app["resources"] if r["name"] == "app-token")["secret"]
    assert secret["permission"] == "READ"


def test_the_sync_excludes_things_that_must_not_be_uploaded(bundle):
    excluded = set(bundle["sync"]["exclude"])
    for pattern in (".venv/**", ".git/**", "**/__pycache__/**"):
        assert pattern in excluded, f"{pattern} would be synced to the workspace"


def test_the_bundle_declares_the_variables_the_resources_use(bundle):
    declared = set(bundle["variables"])
    used = set()
    for path in [*RESOURCES.glob("*.yml"), ROOT / "databricks.yml"]:
        text = path.read_text()
        for name in declared | {"catalog", "schema", "warehouse_id", "app_public_url"}:
            if f"${{var.{name}}}" in text:
                used.add(name)
    assert used <= declared, f"undeclared variables used: {sorted(used - declared)}"


def test_every_model_on_disk_is_in_the_registry():
    """pyproject.toml's [tool.dbx-leaning.models] is what both the wheel
    builder and the requirements exporter read. A package missing from it
    deploys with the wrong dependencies rather than failing."""
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    from _registry import discovered_packages, model_extras

    assert set(discovered_packages()) == set(model_extras()), (
        "models/ and [tool.dbx-leaning.models] disagree"
    )


def test_every_registered_model_has_a_dependency_extra():
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    import tomllib

    from _registry import model_extras

    with (ROOT / "pyproject.toml").open("rb") as fh:
        declared = tomllib.load(fh)["project"]["optional-dependencies"]
    for model, extra in model_extras().items():
        assert extra in declared, f"{model} names extra {extra!r}, which does not exist"


def test_more_models_than_concurrent_slots_is_expected(jobs):
    """The point of the split: models are cheap to add, slots are not. What
    must hold is that no single job can exceed the account ceiling on its
    own — the app enforces the global one."""
    ceiling = 5
    for model in MODELS:
        assert jobs[f"model_{model}"]["max_concurrent_runs"] <= ceiling
