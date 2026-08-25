"""What the Databricks App is given, and what it actually needs.

The App deployment exports its `source_code_path` folder, and that export
**rejects symlinks**, naming one file:

    Failed to export .../DBX_leaning/.venv/bin/python
    INVALID_PARAMETER_VALUE: ... is not an exportable asset. type=symlink

Naming one file makes it read as a problem with that file. It is not.
`.venv` fails first; `frontend/node_modules` holds thousands of symlinks
because that is how pnpm stores packages. The repo root cannot be exported
and no amount of deleting will change that, which is why the app is staged
into its own directory instead.

These tests hold that arrangement in place from both ends: the staged app
must carry everything `app/` imports, and must not carry a symlink.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
STAGE = ROOT / "build" / "app_source"

#: Everything in `scripts/stage_app.py::PACKAGES`, plus what it writes.
STAGED_PACKAGES = ("app", "shared")


def _first_party_imports(package: str) -> set[str]:
    """Top-level repo packages imported anywhere under `package/`.

    Parsed, not grepped: a grep counts a module named in a docstring or a
    comment, and this test's whole value is that it reflects what the code
    really does.
    """
    found: set[str] = set()
    for path in (ROOT / package).rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    # Only names that are directories in this repo are first-party.
    return {name for name in found if (ROOT / name).is_dir() and not name.startswith(".")}


def test_the_staged_app_carries_everything_app_imports():
    """The import graph decides what is staged, not a hand-kept list."""
    needed: set[str] = set()
    for package in STAGED_PACKAGES:
        needed |= _first_party_imports(package)

    missing = sorted(needed - set(STAGED_PACKAGES))
    assert not missing, (
        f"app/ or shared/ imports {missing}, which scripts/stage_app.py does not "
        f"copy — the deployed app would fail at import time"
    )


def test_the_app_does_not_reach_into_the_rest_of_the_repo():
    """The other direction, and the reason the split is cheap.

    If `app/` ever imports `models/` or `job/`, the staged set grows to
    include eleven model packages and the harness — and with them gurobipy,
    torch and ortools, none of which the app has any use for.
    """
    reached = set()
    for package in STAGED_PACKAGES:
        reached |= _first_party_imports(package)
    for forbidden in ("models", "job", "entrypoints", "scripts", "tests"):
        assert forbidden not in reached, f"app/ imports {forbidden}; the app must not"


@pytest.mark.skipif(not STAGE.exists(), reason="no staged app; run scripts/stage_app.py")
def test_the_staged_app_contains_no_symlinks():
    """The export failure this whole arrangement exists to prevent."""
    symlinks = [p.relative_to(STAGE) for p in STAGE.rglob("*") if p.is_symlink()]
    assert not symlinks, f"these cannot be exported to the workspace: {symlinks}"


@pytest.mark.skipif(not STAGE.exists(), reason="no staged app; run scripts/stage_app.py")
def test_the_staged_app_has_what_databricks_apps_looks_for():
    assert (STAGE / "requirements.txt").is_file(), "Apps reads requirements.txt at the app root"
    assert (STAGE / "app" / "main.py").is_file(), "the uvicorn command names app.main:app"
    for package in STAGED_PACKAGES:
        assert (STAGE / package / "__init__.py").is_file(), f"{package} is not importable"


def test_the_bundle_points_the_app_at_the_staged_directory():
    """Not at the repo root, which is what produced the export failure."""
    app = yaml.safe_load((ROOT / "resources" / "app.yml").read_text())
    spec = app["resources"]["apps"]["dbx_leaning"]
    assert spec["source_code_path"] == "../build/app_source", (
        "source_code_path must be the staged app; `../` exports the whole repo "
        "including .venv and frontend/node_modules, which the export rejects"
    )


def test_the_staged_app_is_synced_to_the_workspace():
    bundle = yaml.safe_load((ROOT / "databricks.yml").read_text())
    assert "build/app_source/**" in bundle["sync"]["include"], (
        "the staged app is gitignored, so sync.include is what gets it there"
    )


def test_the_app_is_told_where_the_built_frontend_landed():
    """`frontend/dist` does not exist in the staged layout — `static` does."""
    app = yaml.safe_load((ROOT / "resources" / "app.yml").read_text())
    spec = app["resources"]["apps"]["dbx_leaning"]
    env = {e["name"]: e.get("value") for e in spec["config"]["env"]}
    assert env.get("DBX_FRONTEND_DIST") == "static"
