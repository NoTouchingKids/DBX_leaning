"""`app/app.yaml` and `resources/app.yml` must not drift.

There are two ways this app reaches Databricks, and they read different files:

- `databricks bundle deploy` applies `resources/app.yml`, which can
  interpolate — `${var.catalog}`, `${resources.jobs.model_scenario.id}`.
- Anything else — the Apps UI, `databricks apps deploy --source-code-path ...`,
  a redeploy from the workspace — reads `app/app.yaml` out of the source
  folder and never sees the bundle at all.

Deploying the second way without an `app.yaml` is what produced:

    No command to run and no Python file found.
    Please add a 'command' field to your app.yml file.

Having both files fixes that and creates the obvious next hazard: two
declarations of the same thing, in different syntaxes, that nothing compares.
So these tests compare them.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
APP_YAML = ROOT / "app" / "app.yaml"
BUNDLE = ROOT / "databricks.yml"
RESOURCE = ROOT / "resources" / "app.yml"


def _app_yaml() -> dict:
    return yaml.safe_load(APP_YAML.read_text())


def _resource_config() -> dict:
    spec = yaml.safe_load(RESOURCE.read_text())["resources"]["apps"]["dbx_leaning"]
    return spec["config"]


def _variables() -> dict[str, str]:
    declared = yaml.safe_load(BUNDLE.read_text())["variables"]
    return {name: str(spec.get("default", "")).strip() for name, spec in declared.items()}


def test_the_runtime_file_exists():
    """Without it, a deploy that does not go through the bundle has no command
    and fails before the process starts."""
    assert APP_YAML.is_file(), (
        "app/app.yaml is what Databricks Apps reads from the source folder; "
        "resources/app.yml only applies to a bundle deploy"
    )


def test_the_two_declare_the_same_command():
    assert _app_yaml()["command"] == _resource_config()["command"]


def test_the_command_binds_the_port_the_platform_assigns():
    """A hardcoded port means the platform's health check never connects and
    the deployment is marked FAILED with the app process running fine."""
    command = _app_yaml()["command"]
    assert "$DATABRICKS_APP_PORT" in command, (
        "bind $DATABRICKS_APP_PORT, not a literal — Apps assigns the port"
    )
    assert not any(part.isdigit() for part in command), f"hardcoded port in {command}"


def test_the_ingress_token_comes_from_the_secret_in_both():
    """And in each file's own spelling. `app.yaml` — the file the RUNTIME
    reads — takes `valueFrom`; a bundle resource takes `value_from`. Getting
    either wrong is silent: the app starts with DBX_APP_TOKEN empty, which
    `server/routes/ingest.py::_authorised` treats as accept-everyone.
    """
    runtime = {e["name"]: e for e in _app_yaml()["env"]}
    bundle = {e["name"]: e for e in _resource_config()["env"]}

    assert runtime["DBX_APP_TOKEN"]["valueFrom"] == "app-token"
    assert "value" not in runtime["DBX_APP_TOKEN"], "the token must never be a literal"
    assert bundle["DBX_APP_TOKEN"]["value_from"] == "app-token"


#: `app.yaml` cannot interpolate, so these are literals that must match the
#: bundle variable they stand in for.
MIRRORED = {
    "DBX_CATALOG": "catalog",
    "DBX_SCHEMA": "schema",
    "DBX_WAREHOUSE_ID": "warehouse_id",
    "DBX_APP_PUBLIC_URL": "app_public_url",
}


@pytest.mark.parametrize(("env_name", "variable"), MIRRORED.items())
def test_the_literals_match_the_bundle_variable_they_stand_in_for(env_name, variable):
    runtime = {e["name"]: e.get("value") for e in _app_yaml()["env"]}
    assert runtime[env_name] == _variables()[variable], (
        f"app/app.yaml hardcodes {env_name}; databricks.yml's `{variable}` default "
        "has changed. A hand deploy would now point somewhere the bundle does not."
    )


def test_the_volume_path_is_built_from_the_same_three_names():
    variables = _variables()
    expected = f"/Volumes/{variables['catalog']}/{variables['schema']}/{variables['app_volume']}"
    runtime = {e["name"]: e.get("value") for e in _app_yaml()["env"]}
    assert runtime["DBX_APP_VOLUME"] == expected


def test_job_ids_are_absent_from_the_runtime_file():
    """Deliberately. Job ids do not exist until the bundle creates the jobs,
    so a literal here would be stale the first time a job is recreated —
    pointing the app at a job that no longer exists, which fails at trigger
    time rather than at deploy time. Absent, `/healthz` says so up front.
    """
    runtime = {e["name"] for e in _app_yaml()["env"]}
    assert "DBX_JOB_IDS" not in runtime

    bundle = {e["name"] for e in _resource_config()["env"]}
    assert "DBX_JOB_IDS" in bundle, "the bundle is the only thing that can fill this in"


def test_the_runtime_file_deploys_with_the_app():
    """It has to be inside the source folder to be read at all."""
    assert APP_YAML.parent.name == "app"
    spec = yaml.safe_load(RESOURCE.read_text())["resources"]["apps"]["dbx_leaning"]
    assert spec["source_code_path"] == "../app"
