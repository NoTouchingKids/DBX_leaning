"""The bundle is configuration, which means nothing type-checks it and the
usual failure mode is a name that quietly stopped matching.

These tests bind the three things that must agree — the models on disk, the
jobs in the bundle, and the parameters the app sends — so a rename fails here
rather than at 3am on a real workspace.
"""

from __future__ import annotations

import pathlib
import subprocess
from fnmatch import fnmatch

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
RESOURCES = ROOT / "resources"

# There are no models on this branch, and so no model-job rules to enforce.
#
# Everything that bound "the models on disk" to "the jobs in the bundle" to
# "the parameters the app sends" lived here and is on `dev`, where the models
# are. It comes back with them — the rules were right, they simply have no
# subject yet. What is left below is what is true of the bundle regardless of
# what it deploys.


def load(path: pathlib.Path) -> dict:
    return yaml.safe_load(path.read_text())


@pytest.fixture(scope="module")
def bundle() -> dict:
    return load(ROOT / "databricks.yml")


@pytest.fixture(scope="module")
def all_jobs() -> dict[str, tuple[pathlib.Path, dict]]:
    """Every job the bundle declares, model or not, with the file it came
    from — so the few rules that really are universal can still say so."""
    found = {}
    for path in RESOURCES.glob("*.job.yml"):
        for name, job in load(path)["resources"]["jobs"].items():
            found[name] = (path, job)
    return found


def test_every_job_file_declares_the_job_its_name_promises():
    """A file named `x.job.yml` declares a job keyed `x`.

    Cheap, and it is the check that keeps the `model_*` glob above honest: a
    model job that got misfiled under another name would silently drop out of
    every rule in this module rather than failing one.
    """
    for path in RESOURCES.glob("*.job.yml"):
        expected = path.name.removesuffix(".job.yml")
        declared = list(load(path)["resources"]["jobs"])
        assert declared == [expected], f"{path.name} declares {declared}, expected ['{expected}']"


def test_the_app_declares_no_sql_warehouse_resource(bundle):
    """The read path that needed it is gone — see docs/v4-rewrite-plan.md.

    Worth asserting rather than just deleting the old test: a `sql_warehouse`
    resource grants CAN_USE, and the whole cost model of this platform turns on
    the warehouse staying asleep. Something that can wake it should have to
    argue for itself.
    """
    app = load(RESOURCES / "app.yml")["resources"]["apps"]["dbx_leaning"]
    kinds = [k for r in app.get("resources", []) for k in r if k != "name" and k != "description"]
    assert "sql_warehouse" not in kinds, "the app is asking for a warehouse again"


def test_no_resource_is_declared_that_a_deploy_cannot_validate(bundle):
    """A declared app resource is checked at DEPLOY time, so one naming
    something absent fails the whole deploy before anything is uploaded —
    which is how `oauth-client-secret` once 404'd a deploy for being an opt-in
    feature nobody had opted into.

    Everything declared here must therefore be something the deploy is certain
    to find: a job this same bundle creates, the volume, the ingress secret, or
    the warehouse the app cannot work without anyway. Lakebase is the one that
    may legitimately not exist yet (`lakebase_host` defaults to empty), so it
    stays commented out.
    """
    app = load(RESOURCES / "app.yml")["resources"]["apps"]["dbx_leaning"]
    kinds = {k for r in app["resources"] for k in r if k not in {"name", "description"}}
    assert "database" not in kinds and "postgres" not in kinds, (
        "a Lakebase resource fails the deploy when no instance exists; keep it "
        "opt-in, like the SP secret"
    )


def test_every_declared_secret_is_read_only_and_never_a_literal(bundle):
    """A credential travels by reference or not at all.

    There are no secret resources at all right now — `app-token` went with the
    app's own ingress check, since the Apps proxy already refuses anything
    without a Databricks OAuth token from a CAN_USE principal. So this passes
    vacuously, deliberately: it is the rule the next secret has to meet, and
    the reason it is worth keeping is that a declared secret is validated at
    DEPLOY time, so naming a key that is not in the scope 404s the whole
    deploy before anything is uploaded.
    """
    app = load(RESOURCES / "app.yml")["resources"]["apps"]["dbx_leaning"]
    env = {e["name"]: e for e in app["config"]["env"]}
    declared = {r["name"]: r for r in app.get("resources", []) if "secret" in r}

    for name, entry in env.items():
        if "value_from" in entry:
            assert entry["value_from"] in declared, (
                f"{name} reads secret {entry['value_from']!r}, which is not "
                f"declared under `resources:` — the deploy fails at validation"
            )
    for name, resource in declared.items():
        assert resource["secret"]["permission"] == "READ", (
            f"{name} asks for more than it needs; an app reads its secrets"
        )


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


def test_the_sync_excludes_things_that_must_not_be_uploaded(bundle):
    """Two of these are not tidiness. The App export rejects symlinks and
    fails on the FIRST one it meets, so the count is beside the point:
    `.venv/bin` is full of them and `app/client/node_modules` carries bin
    shims whatever installs it. Either directory reaching the workspace
    fails the whole deploy.
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


def test_the_bundle_declares_the_variables_the_resources_use(bundle):
    declared = set(bundle["variables"])
    used = set()
    for path in [*RESOURCES.glob("*.yml"), ROOT / "databricks.yml"]:
        text = path.read_text()
        for name in declared | {"catalog", "schema", "warehouse_id", "app_public_url"}:
            if f"${{var.{name}}}" in text:
                used.add(name)
    assert used <= declared, f"undeclared variables used: {sorted(used - declared)}"


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


def test_the_optional_service_principal_secret_is_not_declared_by_default(bundle):
    """`oauth-client-secret` must ship COMMENTED OUT, and this is the test that
    keeps it that way.

    A declared secret resource is validated when the app is updated, not when
    it is read — so declaring one whose key is not in the scope fails the whole
    deploy before a single file is uploaded:

        Invalid secret resource oauth-client-secret: Secret with scope
        dbx-leaning and key oauth-client-secret does not exist. (404)

    Every other optional thing here degrades at run time and says so on
    /healthz. This one cannot, so the default must not require it. Someone
    running as their own principal uncomments two blocks and creates the
    secret; nobody else pays for the feature.
    """
    app = load(RESOURCES / "app.yml")["resources"]["apps"]["dbx_leaning"]
    env = {e["name"]: e for e in app["config"]["env"]}

    declared = [r["name"] for r in app["resources"]]
    assert "oauth-client-secret" not in declared, (
        "declaring this makes it REQUIRED — a deploy without the secret 404s"
    )
    assert "DBX_OAUTH_CLIENT_SECRET" not in env, (
        "a value_from pointing at an undeclared resource fails the same way"
    )

    # The id stays: it is an identifier, not a credential, and on its own it
    # is inert — `has_client_credentials` needs both, and services.py reports
    # the half-configured case on /healthz rather than silently falling back.
    assert env["DBX_OAUTH_CLIENT_ID"]["value"] == "${var.oauth_client_id}"


def test_the_commented_secret_block_still_says_value_from(bundle):
    """The instructions someone uncomments have to be correct when they do.

    `valueFrom` is right in app.yaml and wrong here, and a commented block is
    exactly where that rots unnoticed — nothing parses it.
    """
    text = (RESOURCES / "app.yml").read_text()
    assert "#   value_from: oauth-client-secret" in text
    assert "valueFrom: oauth-client-secret" not in text


def test_no_variable_holds_a_credential(bundle):
    """Belt and braces over the test above: nothing declared in `variables:`
    may look like a secret, whatever it is called.

    The exemptions read by SUFFIX rather than by substring, and the line they
    draw is between naming WHERE a credential lives and holding one:

        *_scope        the name of a secret scope
        *_secret_key   the key within it

    Neither is a credential — both have to be variables so a target can point
    at a different scope — while `..._secret` or `..._token` would be the value
    itself, which belongs under `resources:` as a declared secret because
    variables land in the deployment state.
    """
    for name in bundle["variables"]:
        if name.endswith("_scope") or name.endswith("_secret_key"):
            continue
        assert not any(word in name for word in ("secret", "password", "token", "credential")), (
            f"variable {name!r} looks like a credential; variables land in the "
            "deployment state — declare it under `resources:` as a secret"
        )


# --- the package manager ---------------------------------------------------

CLIENT = ROOT / "app" / "client"


def test_nothing_invokes_a_bun_builtin_as_though_it_were_a_script():
    """`bun <name>` is shorthand for `bun run <name>` ONLY when the name is not
    a bun builtin — and `test` and `build` both are.

    `bun test` runs Bun's own test runner, which collects none of the vitest
    suite and reports success having run nothing. `bun build` is Bun's bundler,
    not `tsc -b && vite build`. Both fail by doing something plausible instead
    of erroring, so this reads every tracked file rather than trusting anyone
    to remember which names collide.
    """

    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()

    offenders = []
    for name in tracked:
        path = ROOT / name
        if not path.is_file() or "lock" in path.name:
            continue
        # This file states the rule, so it necessarily quotes what the rule
        # forbids. Excluding it is not a loophole — there is nothing here for
        # anyone to run.
        if path == pathlib.Path(__file__).resolve():
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, IsADirectoryError):
            continue
        for builtin in ("test", "build", "install", "add", "remove", "update"):
            if f"bun {builtin}" in text and builtin != "install":
                offenders.append(f"{name}: 'bun {builtin}'")

    assert not offenders, (
        "these invoke a bun builtin where a package script was meant; "
        f"use `bun run <script>`: {offenders}"
    )


def test_lakebase_connects_as_the_principal_whose_token_it_presents(bundle):
    """`lakebase_user` and `oauth_client_id` must be the same application id.

    Lakebase takes an OAuth token as its password, and the Postgres role
    Databricks provisions is named after the principal that token belongs to.
    Connecting as one principal while presenting another's token fails as a
    plain authentication error — indistinguishable from a wrong secret, so the
    hour is spent looking at the secret scope.

    Either may be empty: no Lakebase configured, or the app running as the
    service principal Apps injects for it. It is the two disagreeing that is
    always wrong.
    """
    variables = {
        name: str(spec.get("default", "")).strip() for name, spec in bundle["variables"].items()
    }
    user, client = variables["lakebase_user"], variables["oauth_client_id"]
    if not user or not client:
        return
    assert user == client, (
        f"lakebase_user is {user!r} but the app authenticates as {client!r}. "
        "The Lakebase role is named after the principal whose token is "
        "presented; these must be the same application id."
    )
