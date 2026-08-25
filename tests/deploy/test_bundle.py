"""The bundle is configuration, which means nothing type-checks it and the
usual failure mode is a name that quietly stopped matching.

These tests bind the three things that must agree — the models on disk, the
jobs in the bundle, and the parameters the app sends — so a rename fails here
rather than at 3am on a real workspace.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
from fnmatch import fnmatch

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
RESOURCES = ROOT / "resources"
REQUIREMENTS = ROOT / "deploy" / "requirements"

#: The model packages, discovered rather than listed.
MODELS = sorted(
    p.name
    for p in (ROOT / "job" / "models").iterdir()
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
        assert model in MODELS, f"job {name} refers to a model that is not in job/models/"


def test_each_job_runs_its_own_model(jobs):
    for model in MODELS:
        params = {p["name"]: p["default"] for p in jobs[f"model_{model}"]["parameters"]}
        assert params["DBX_MODEL"] == f"job.models.{model}"


def test_jobs_declare_exactly_the_parameters_the_app_sends(jobs):
    """Databricks rejects a run-now parameter a job has not declared, so this
    drift would surface as every trigger failing."""
    from server.routes.runs import JOB_PARAMETER_NAMES

    for model in MODELS:
        declared = {p["name"] for p in jobs[f"model_{model}"]["parameters"]}
        assert declared == set(JOB_PARAMETER_NAMES), (
            f"model_{model} declares {sorted(declared)}, app sends {sorted(JOB_PARAMETER_NAMES)}"
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
        assert deps == [f"-r ${{workspace.file_path}}/deploy/requirements/{model}.txt"], (
            f"model_{model} deps: {deps}"
        )
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
    # Both spellings are real, in different files. `app.yaml` — the file the
    # Apps runtime reads — takes `valueFrom`; a BUNDLE resource takes
    # `value_from`, and the CLI generates the former from the latter.
    # `databricks bundle schema` defines only `value_from` here, and getting
    # it wrong does not fail the deploy: CLI v1.13.0 warns that "The
    # 'valueFrom' field will be ignored" and carries on, so the app starts
    # with no token — which `_authorised` treats as "accept everyone".
    assert env["DBX_APP_TOKEN"]["value_from"] == "app-token", (
        "a bundle resource takes value_from; valueFrom is the app.yaml "
        "spelling, is ignored here, and leaves the ingress open"
    )
    assert "valueFrom" not in env["DBX_APP_TOKEN"], "ignored here, and silently"
    secret = next(r for r in app["resources"] if r["name"] == "app-token")["secret"]
    assert secret["permission"] == "READ"


def test_the_app_is_deployed_from_the_app_folder(bundle):
    """`app/` holds everything the app needs and nothing else — `server/`,
    `client/`, `dist/`, a copy of `shared/`, and its own requirements.txt.

    It is a real tracked directory, not something a staging step assembles:
    a staged directory is build output, so it is gitignored, absent from a
    fresh checkout, and absent from a Databricks Git folder — where the deploy
    may be driven from inside the workspace and only tracked files exist.
    """
    app = load(RESOURCES / "app.yml")["resources"]["apps"]["dbx_leaning"]
    assert app["source_code_path"] == "../app"

    # The command names the package INSIDE that folder, not the folder.
    assert app["config"]["command"][:2] == ["uvicorn", "server.main:app"]


def test_the_app_folder_carries_everything_it_needs():
    """Nothing outside `app/` travels, so everything `server/` imports has to
    be in there. `server/` imports exactly one first-party package: `shared`.
    """
    for rel in ("server/main.py", "shared/envelope.py", "requirements.txt", "dist/index.html"):
        assert (ROOT / "app" / rel).exists(), f"app/{rel} is missing from the deployed folder"


def test_the_sync_excludes_things_that_must_not_be_uploaded(bundle):
    """Two of these are not tidiness. The App export rejects symlinks, naming
    one file — and `.venv/bin/python` is only the first of them, with
    `frontend/node_modules` holding thousands because that is how pnpm stores
    packages. Either one reaching the workspace fails the whole deploy.
    """
    excluded = set(bundle["sync"]["exclude"])
    for pattern in (
        ".venv/**",
        "app/client/**",
        ".git/**",
        "**/__pycache__/**",
    ):
        assert pattern in excluded, f"{pattern} would be synced to the workspace"


def test_the_built_frontend_is_not_excluded(bundle):
    """`app/server/spa.py` serves `dist/`, and nothing in the workspace can build it.

    This used to need a `sync.include` and a rule that no exclude contradict
    it, because dist lived inside the client tree — so `app/client/**` could not be
    excluded wholesale and every build-time config had to be named one at a
    time. `vite.config.ts` now writes `../dist`, at the app root, and the
    conflict is gone: assert it stays gone.
    """
    for pattern in bundle["sync"]["exclude"]:
        assert not fnmatch("app/dist/index.html", pattern), (
            f"exclude {pattern!r} drops the built SPA; the app would answer "
            "503 on every page while the API worked"
        )
        assert not fnmatch("app/dist/assets/index.js", pattern), (
            f"exclude {pattern!r} drops the SPA assets"
        )


def test_the_app_is_told_where_the_built_frontend_landed(bundle):
    """`DBX_FRONTEND_DIST` and `vite.config.ts`'s `outDir` are two halves of
    one decision, written in different languages. Neither file can see the
    other, so this is what holds them together.
    """
    app = load(RESOURCES / "app.yml")["resources"]["apps"]["dbx_leaning"]
    env = {e["name"]: e.get("value") for e in app["config"]["env"]}
    assert env.get("DBX_FRONTEND_DIST") == "dist"

    vite = (ROOT / "app" / "client" / "vite.config.ts").read_text()
    assert 'outDir: "../dist"' in vite, (
        "vite must write the app root's dist/; DBX_FRONTEND_DIST points there"
    )


def test_the_built_frontend_is_tracked_by_git():
    """Committed build output, which is unusual and load-bearing here.

    A deploy driven from inside Databricks — a Git folder, a notebook — has no
    Node runtime and sees only tracked files. A gitignored bundle would simply
    not be there, and every page would answer 503 while the API worked fine.
    """
    tracked = subprocess.run(
        ["git", "ls-files", "app/dist"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert "app/dist/index.html" in tracked, (
        "the built SPA must be committed; run `pnpm build` in app/client/"
    )
    assert not [f for f in tracked if f.endswith(".map")], (
        "sourcemaps are 4x the bundle and regenerate on every build"
    )


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
        "job/models/ and [tool.dbx-leaning.models] disagree"
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


def _declared_results_tables() -> dict[str, str]:
    """Each model's `results_table`, read without importing the model.

    Importing would pull gurobipy, torch and emcee into the test process for a
    string constant. The attribute is a plain class-level literal in every
    model, so the source is a faithful and much cheaper source of truth.
    """
    found: dict[str, str] = {}
    for name in MODELS:
        source = (ROOT / "job" / "models" / name / "model.py").read_text()
        match = re.search(r'^\s*results_table\s*=\s*"([^"]+)"', source, re.MULTILINE)
        if match:
            found[name] = match.group(1)
    return found


def test_every_model_declares_a_results_table():
    declared = _declared_results_tables()
    missing = sorted(set(MODELS) - set(declared))
    assert not missing, f"no results_table declared in job/models/<name>/model.py: {missing}"


def test_every_model_results_table_exists_in_the_ddl():
    """The one link in the chain that nothing else checks.

    `tests/deploy` binds models to the registry, to extras, to requirements,
    to job files and to DBX_JOB_IDS — six links, thoroughly. The DDL was not
    one of them and no test anywhere referenced `002_model_results.sql`. So a
    new model could pass every test in this suite and then fail its first real
    write with TABLE_OR_VIEW_NOT_FOUND: on a workspace, inside a job, at the
    end of a long run, having already consumed one of five account-wide task
    slots.

    The failure is also silent in the worst way — `emit("result", rows=...)`
    is the one message type that is explicitly NOT best-effort, so a run whose
    result write fails must not report SUCCEEDED. Catching it here costs
    nothing.
    """
    ddl = (ROOT / "uc_ddl" / "002_model_results.sql").read_text()
    created = set(re.findall(r"CREATE TABLE IF NOT EXISTS\s+\S+\.(\w+)\s*\(", ddl))

    missing = {
        model: table for model, table in _declared_results_tables().items() if table not in created
    }
    assert not missing, (
        f"declared results_table with no CREATE TABLE in uc_ddl/002_model_results.sql: "
        f"{missing}. The run would fail on its first result write, on a workspace."
    )


def test_the_ddl_creates_no_results_table_no_model_claims():
    """The other direction: a table left behind by a renamed or deleted model.

    Harmless at run time, which is exactly why it accumulates.
    """
    ddl = (ROOT / "uc_ddl" / "002_model_results.sql").read_text()
    created = set(re.findall(r"CREATE TABLE IF NOT EXISTS\s+\S+\.(results_\w+)\s*\(", ddl))
    claimed = set(_declared_results_tables().values())
    orphans = sorted(created - claimed)
    assert not orphans, f"results tables no model declares: {orphans}"


#: The parts `app/server/config.py::_lakebase_dsn` assembles a connection string from.
#: `DBX_LAKEBASE_DSN` is the alternative whole-string form and is not used here.
LAKEBASE_ENV = (
    "DBX_LAKEBASE_HOST",
    "DBX_LAKEBASE_DATABASE",
    "DBX_LAKEBASE_PORT",
    "DBX_LAKEBASE_USER",
)


def bundle_app_spec() -> dict:
    """The app resource, read from its own file rather than the fixture, so
    these tests do not depend on how the jobs fixture merges resources."""
    return load(RESOURCES / "app.yml")["resources"]["apps"]["dbx_leaning"]


def _app_env(bundle_app: dict) -> dict[str, str]:
    return {e["name"]: e.get("value", "") for e in bundle_app["config"]["env"]}


def test_the_app_is_wired_for_lakebase(bundle):
    """`run_status` lives in Postgres, and the bundle has to say where.

    The code for this shipped long before the deploy configuration did: the
    app read `DBX_LAKEBASE_*`, `resources/app.yml` set none of them, so a
    deployed app resolved no DSN and silently fell back to the
    warehouse-backed store. Nothing failed — it just quietly stopped being
    the design `CLAUDE.md` describes.

    That fallback matters because `WarehouseRunStore.release_slot` is a
    documented no-op relying on reconciliation, so the account-wide
    5-concurrent-task ceiling degrades from transactional to advisory.
    """
    app = bundle_app_spec()
    env = _app_env(app)
    missing = [name for name in LAKEBASE_ENV if name not in env]
    assert not missing, f"app.yml sets no {missing} — the app cannot find Lakebase"


def test_every_lakebase_setting_comes_from_a_declared_variable(bundle):
    """Wired to variables, not literals: the instance differs per target, and
    a hostname baked into a committed file is the wrong kind of default."""
    env = _app_env(bundle_app_spec())
    declared = set(bundle["variables"])
    for name in LAKEBASE_ENV:
        value = env[name]
        assert value.startswith("${var."), f"{name} is a literal: {value!r}"
        var = value.removeprefix("${var.").removesuffix("}")
        assert var in declared, f"{name} references undeclared variable {var!r}"


def test_an_unconfigured_lakebase_host_is_a_supported_deploy(bundle):
    """Empty must stay the default.

    A required host would make the bundle undeployable until someone
    provisions Lakebase, and the app genuinely does work without it — the
    fallback is degraded, not broken. `services.py` records the choice and
    `/healthz` reports it, which is what keeps "degraded" from being silent.
    """
    assert bundle["variables"]["lakebase_host"]["default"] == ""


def test_no_lakebase_credential_is_carried_as_a_bundle_variable(bundle):
    """A password in a variable lands in the deployment state.

    Lakebase authenticates with a short-lived OAuth token, which is why
    `app/server/store.py` connects per operation rather than pooling. If an explicit
    credential is ever needed it belongs in a secret, the way `app-token`
    already is — never here.
    """
    for name, spec in bundle["variables"].items():
        if "lakebase" not in name:
            continue
        assert "password" not in name, f"variable {name!r} looks like a credential"
        assert "secret" not in str(spec.get("default", "")).lower()


def test_each_job_names_its_environment_after_its_model(jobs):
    """A cosmetic convention, and worth pinning because it drifted once.

    `environment_key` only has to be self-consistent within a file, so naming
    one after the dependency EXTRA instead of the model works and nothing
    catches it. Two of eleven had drifted that way, which leaves someone
    scanning the directory wondering whether the difference means something.
    """
    for name, job in jobs.items():
        model = name.removeprefix("model_")
        declared = job["environments"][0]["environment_key"]
        assert declared == model, f"{name}: environment named {declared!r}, expected {model!r}"
        for task in job["tasks"]:
            used = task["environment_key"]
            assert used == model, f"{name}: task uses environment {used!r}"


def test_the_service_principal_secret_is_a_secret_not_a_variable(bundle):
    """The app authenticates as one service principal against Postgres, Unity
    Catalog and the Jobs API. Its id is not a credential; its secret is.

    A bundle variable lands in the deployment state, so a credential there is
    readable by anyone who can read the bundle's state — which is not the same
    set of people as those who can read the secret scope.
    """
    app = load(RESOURCES / "app.yml")["resources"]["apps"]["dbx_leaning"]
    env = {e["name"]: e for e in app["config"]["env"]}

    assert "value" not in env["DBX_OAUTH_CLIENT_SECRET"], "never a literal"
    assert env["DBX_OAUTH_CLIENT_SECRET"]["value_from"] == "oauth-client-secret"
    secret = next(r for r in app["resources"] if r["name"] == "oauth-client-secret")["secret"]
    assert secret["permission"] == "READ"

    # The id may be a variable — it is an identifier, not a credential.
    assert env["DBX_OAUTH_CLIENT_ID"]["value"] == "${var.oauth_client_id}"


def test_no_variable_holds_a_credential(bundle):
    """Belt and braces over the test above: nothing declared in `variables:`
    may look like a secret, whatever it is called.

    `app_secret_scope` is exempt and is the reason this reads by suffix rather
    than by substring — the NAME of a secret scope is not itself a secret, and
    has to be a variable so a target can point at a different one.
    """
    for name in bundle["variables"]:
        if name.endswith("_scope"):
            continue
        assert not any(word in name for word in ("secret", "password", "token", "credential")), (
            f"variable {name!r} looks like a credential; variables land in the "
            "deployment state — declare it under `resources:` as a secret"
        )
