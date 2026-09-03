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


def test_a_secret_backed_env_uses_each_file_s_own_spelling():
    """BOTH SPELLINGS ARE REAL, in different files, and getting it wrong is
    silent:

        app.yaml         (what the Apps runtime reads)  ->  valueFrom
        resources/*.yml  (what the bundle declares)     ->  value_from

    `databricks bundle schema` defines only `value_from` under
    `resources.apps.*.config.env`, and the CLI translates it. Spelling it
    `valueFrom` there does not fail the deploy — CLI v1.13.0 warns that "The
    'valueFrom' field will be ignored" and carries on, leaving the variable
    empty at runtime.

    This used to pin one key, `DBX_APP_TOKEN`. That env is gone with the
    ingress secret, so the test is generalised rather than deleted: it holds
    for whatever secret comes next (a Lakebase password is the likely one) and
    passes vacuously until then. The trap outlived the token.
    """
    runtime = {e["name"]: e for e in _app_yaml()["env"]}
    bundle = {e["name"]: e for e in _resource_config()["env"]}

    for name, entry in runtime.items():
        assert "value_from" not in entry, (
            f"{name} in app.yaml uses the BUNDLE spelling; the runtime reads "
            f"valueFrom and would see nothing"
        )
    for name, entry in bundle.items():
        assert "valueFrom" not in entry, (
            f"{name} in resources/app.yml uses the app.yaml spelling; the CLI "
            f"ignores it with a warning and the app starts without it"
        )
        if "value_from" in entry:
            assert "value" not in entry, f"{name} is both a secret and a literal"


#: `app.yaml` cannot interpolate, so these are literals that must match the
#: bundle variable they stand in for.
MIRRORED = {
    "DBX_CATALOG": "catalog",
    "DBX_SCHEMA": "schema",
    "DBX_APP_PUBLIC_URL": "app_public_url",
    "DBX_LAKEBASE_SCHEMA": "lakebase_schema",
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


def test_job_ids_are_absent_from_both_files():
    """v4 discovers jobs by tag, so neither file should name any.

    This test used to assert the opposite of half of itself: absent from the
    runtime file, PRESENT in the bundle, because only the bundle could
    interpolate `${resources.jobs.model_x.id}`. That is the coupling v4
    removes — the app now finds its jobs by their `project: dbx-leaning` tag,
    which works however the app was deployed and wherever the jobs are defined.

    `DBX_JOB_IDS` is still read if something sets it, as an allow-list. What
    must not come back is anything PRODUCING it, because that is what ties the
    app's deploy to the jobs'.
    """
    for where, env in (("app/app.yaml", _app_yaml()), ("resources/app.yml", _resource_config())):
        assert "DBX_JOB_IDS" not in {e["name"] for e in env["env"]}, (
            f"{where} declares DBX_JOB_IDS; v4 discovers jobs by tag instead"
        )


def test_the_app_asks_for_no_sql_warehouse():
    """v4's app queries no warehouse: run state is Postgres, live gaps are
    replayed by the job, and history arrives via the ingestion job. A
    DBX_WAREHOUSE_ID here would be a warehouse someone eventually wakes up."""
    for where, env in (("app/app.yaml", _app_yaml()), ("resources/app.yml", _resource_config())):
        assert "DBX_WAREHOUSE_ID" not in {e["name"] for e in env["env"]}, (
            f"{where} still hands the app a warehouse id"
        )


def test_the_runtime_file_deploys_with_the_app():
    """It has to be inside the source folder to be read at all."""
    assert APP_YAML.parent.name == "app"
    spec = yaml.safe_load(RESOURCE.read_text())["resources"]["apps"]["dbx_leaning"]
    assert spec["source_code_path"] == "../app"
