"""Everything `server/` imports has to be INSIDE `app/`.

`resources/app.yml` hands Databricks Apps `../app` as its `source_code_path`,
and nothing above that folder travels. So an import in `server/` that resolves
locally from the repo root but has no counterpart under `app/` passes every
test here and fails at startup on the workspace, where the only symptom is the
app going unhealthy.

That is not hypothetical. Deleting `app/shared/` — a tracked copy that looked
like pure duplication next to the repo root's `shared/` — broke exactly this,
and the suite stayed green because pytest's `pythonpath` had the repo root on
it. The fix was to move `shared/` INTO `app/` and map it back out for the job
(`[tool.setuptools] package-dir` in pyproject.toml), so there is one copy and
it lives where the deployed app needs it.

`tests/deploy/test_shared_copy.py`, which policed the two generated copies,
is gone: there are no copies left to drift.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
APP = ROOT / "app"

#: Third-party and stdlib are installed from `app/requirements.txt`; only
#: first-party packages have to physically travel.
FIRST_PARTY = {"server", "shared"}


def _top_level_imports(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


@pytest.mark.parametrize(
    "module", sorted((APP / "server").rglob("*.py")), ids=lambda p: str(p.relative_to(APP))
)
def test_every_first_party_import_resolves_inside_app(module: pathlib.Path):
    for name in _top_level_imports(module) & FIRST_PARTY:
        assert (APP / name).is_dir(), (
            f"{module.relative_to(ROOT)} imports {name!r}, which is not under app/. "
            f"The deployed app is that folder and nothing above it, so this import "
            f"works here and fails on the workspace."
        )


def test_the_job_gets_shared_from_the_same_place_the_app_does():
    """One `shared/`, two consumers, no copy.

    `shared` lives under `app/` because the app cannot reach outside it. The
    job has no such limit — it installs this repo as a distribution — so the
    mapping is what gives it the same module rather than a second copy of it.
    """
    import tomllib

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_dir = pyproject["tool"]["setuptools"]["package-dir"]

    assert package_dir.get("shared") == "app/shared"
    assert "shared" in pyproject["tool"]["setuptools"]["packages"]
    assert not (ROOT / "shared").exists(), (
        "a second shared/ at the repo root is the copy this layout exists to avoid"
    )


def test_no_model_is_a_dependency_of_the_platform():
    """A model belongs in the dev group, never in `[project.dependencies]`.

    Two reasons, and the second is the one that bites. The platform depends on
    no particular model — the harness finds whatever is installed, by entry
    point. And the job environment installs this repo's root with PIP, which
    reads none of `[tool.uv.sources]`: a workspace member named there is
    resolvable by uv and by nothing else, so a model in the runtime dependency
    list sends the deploy looking for it on PyPI.
    """
    import tomllib

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    runtime = " ".join(
        pyproject["project"]["dependencies"]
        + [d for extra in pyproject["project"]["optional-dependencies"].values() for d in extra]
    )
    workspace_only = set(pyproject["tool"]["uv"]["sources"])

    for name in workspace_only:
        assert name not in runtime, (
            f"{name!r} is resolvable only through [tool.uv.sources]; pip cannot "
            f"find it, so the job environment's install of this repo would fail"
        )
