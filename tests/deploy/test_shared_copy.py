"""`app/shared/` and `job/shared/` must be faithful copies of `shared/`.

Each deployable unit is a folder that carries everything it needs, so the one
package both units import has to exist inside both. `app/` is the sharp case:
`resources/app.yml` gives Databricks Apps that folder as its
`source_code_path`, nothing outside it travels, and `app/server/` imports
`shared` on the first line it runs. `job/`'s copy is not load-bearing yet — a
job task runs `entrypoints/run_model.py` out of the whole synced tree — and is
there so the folder is already complete when it becomes a wheel.

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


@pytest.mark.parametrize("copy", COPIES, ids=str)
def test_the_copy_is_tracked_by_git(copy):
    """Not generated at deploy time. A deploy driven from inside Databricks —
    a Git folder, a notebook — sees only tracked files."""
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files", str(copy)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert f"{copy}/envelope.py" in tracked


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
