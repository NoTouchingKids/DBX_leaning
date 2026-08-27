"""`app/shared/` and `job/shared/` must be faithful copies of `shared/`.

Each deployable unit is a folder that carries everything it needs, so the one
package both units import has to exist inside both, and both copies are
load-bearing. `resources/app.yml` gives Databricks Apps `app/` as its
`source_code_path` and nothing outside it travels; `job/*.py` imports
`.shared` — relative, its own copy — so `job` is one importable package from
anywhere its parent is on `sys.path`.

Where they differ is whether the copy is committed:

- `app/shared/` is TRACKED, and has to be. An app can be deployed without this
  bundle at all — the Apps UI, `databricks apps deploy --source-code-path ...`
  — and those see only what is in git.
- `job/shared/` is GENERATED and gitignored. A job is only ever deployed by
  `databricks bundle deploy`, whose `preinit` hook runs the sync before
  syncing anything, so the copy belongs in the workspace and not in the
  history.

Neither can be a symlink: the workspace export rejects those, which is the
failure that started this whole line of work.

Duplication's failure mode is drift, and drift here is nasty in a specific
way: the tests would pass against the canonical copy while the deployed app
ran the stale one, so the envelope contract would hold everywhere except in
production. That is what these tests exist to make impossible.

The duplication is scoped to this stage. Packaging `shared` as a wheel
retires the copies and this file.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import sync_shared  # noqa: E402

#: Relative to the repo root, so failure messages name a path you can open.
COPIES = [t.relative_to(ROOT) for t in sync_shared.TARGETS]


@pytest.mark.parametrize("copy", COPIES, ids=str)
def test_the_copy_matches_the_canonical_shared(copy):
    drift = sync_shared.differences(ROOT / copy)
    assert drift == [], (
        f"{copy} has drifted from shared/: "
        + ", ".join(drift)
        + " — run `uv run python scripts/sync_shared.py`"
    )


@pytest.mark.parametrize("copy", COPIES, ids=str)
def test_the_copy_exists_at_all(copy):
    """A missing copy is not a drift report, it is a deploy that cannot boot:
    `server/main.py` imports `shared.envelope` on the first line it runs."""
    assert (ROOT / copy / "envelope.py").is_file()


#: The copies that are made at deploy time instead of committed.
GENERATED = [t.relative_to(ROOT) for t in sync_shared.GENERATED]
TRACKED = [c for c in COPIES if c not in GENERATED]


def _tracked(path) -> list[str]:
    import subprocess

    return subprocess.run(
        ["git", "ls-files", str(path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()


@pytest.mark.parametrize("copy", TRACKED, ids=str)
def test_the_app_copy_is_tracked_by_git(copy):
    """It cannot be generated. An app can be deployed without this bundle —
    the Apps UI, `databricks apps deploy --source-code-path ...` — and those
    see only committed files, so a gitignored copy would not be there and
    `server/main.py` would fail on the first line it runs."""
    assert f"{copy}/envelope.py" in _tracked(copy)


@pytest.mark.parametrize("copy", GENERATED, ids=str)
def test_the_job_copy_is_not_tracked_by_git(copy):
    """The opposite requirement, and for a reason: a job is only ever deployed
    by `databricks bundle deploy`, which makes this copy on the way. Committing
    it would put a second `shared/` in the history for no deploy to need."""
    assert _tracked(copy) == [], f"{copy} is generated at deploy time — `git rm -r --cached {copy}`"


@pytest.mark.parametrize("copy", GENERATED, ids=str)
def test_the_generated_copy_is_gitignored(copy):
    """Untracked is not enough. Without an ignore rule it shows up as noise in
    every `git status` and is one `git add -A` away from being committed."""
    import subprocess

    ignored = subprocess.run(
        ["git", "check-ignore", f"{copy}/envelope.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert ignored.returncode == 0, f"add /{copy}/ to .gitignore"


def test_the_bundle_makes_the_generated_copy_before_it_syncs():
    """The hook is what turns "gitignored" from a broken deploy into a working
    one, so its absence must fail here rather than in the workspace.

    `experimental.scripts` and not the top-level `scripts:` block: in CLI
    v1.13.0 only the former actually fires, and a hook that silently does not
    run would sync a `job/` with no `shared/` inside it.
    """
    import yaml

    bundle = yaml.safe_load((ROOT / "databricks.yml").read_text())
    scripts = bundle.get("experimental", {}).get("scripts", {})
    assert "sync_shared.py" in scripts.get("preinit", ""), (
        "databricks.yml must run scripts/sync_shared.py as an experimental "
        "preinit hook, or job/shared/ never reaches the workspace"
    )


def test_a_fresh_checkout_can_still_import_the_job():
    """`conftest.py` at the repo root makes the missing copy before collection.

    Without it, a clean clone fails to collect a dozen unrelated files with an
    ImportError about `job.shared` — which names the symptom and nothing about
    the cause.
    """
    assert (ROOT / "conftest.py").is_file()
    assert "sync_shared.ensure()" in (ROOT / "conftest.py").read_text()


@pytest.mark.parametrize("folder", ["app", "job"], ids=str)
def test_the_deployable_folder_holds_no_symlink(folder):
    """The one thing the workspace export cannot handle."""
    symlinks = [
        p for p in (ROOT / folder).rglob("*") if p.is_symlink() and "node_modules" not in p.parts
    ]
    assert symlinks == [], f"{folder}/ cannot contain symlinks: {symlinks}"


def test_imports_resolve_to_the_canonical_shared_not_the_copy():
    """`pythonpath` in pyproject.toml puts the repo root ahead of `app/`.

    With the order reversed every test in this suite would exercise the COPY,
    and `test_the_copy_matches_the_canonical_shared` would be the only thing
    standing between that and a silent divergence. Assert the order directly.
    """
    import shared

    assert pathlib.Path(shared.__file__).parent == ROOT / "shared", (
        "tests are importing app/shared, the deployed copy, not shared/"
    )


@pytest.mark.parametrize("copy", COPIES, ids=str)
@pytest.mark.parametrize("name", ["envelope", "protocol", "schema", "tables", "codec", "seq"])
def test_every_module_the_units_import_is_in_the_copy(copy, name):
    assert (ROOT / copy / f"{name}.py").is_file()
